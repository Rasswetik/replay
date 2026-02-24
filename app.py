"""
Telethon Relay Service for Luna Gifts
Runs on Render.com (free tier) to bypass PythonAnywhere TCP restrictions.
Exposes HTTP API for: send-code, sign-in, disconnect, send-gift, status.
"""

import os
import json
import asyncio
import logging
import base64
import threading
import time
import urllib.request
import urllib.error
from flask import Flask, request, jsonify

# ---------- Telethon ----------
from telethon import TelegramClient, errors as tl_errors
from telethon.sessions import StringSession
from telethon.tl import functions as tl_functions, types as tl_types
from telethon.tl.functions.auth import ExportLoginTokenRequest, ImportLoginTokenRequest
from telethon.tl.types.auth import LoginToken, LoginTokenMigrateTo, LoginTokenSuccess

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# Secret key for authenticating requests from PA app
RELAY_SECRET = os.environ.get('RELAY_SECRET', 'change-me-in-production')

# PythonAnywhere URL for session backup (survives Render restarts)
PA_URL = os.environ.get('PA_URL', 'https://lunagifts.pythonanywhere.com')

# Persistent session data file (local, may be wiped on Render restart)
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
SESSION_FILE = os.path.join(DATA_DIR, 'session.json')

# Temp auth state (in-memory, per-process)
_auth_state = {}

# Flag: have we tried restoring from PA backup this process?
_pa_restore_attempted = False


def _check_secret():
    """Validate relay secret from request."""
    token = (request.json or {}).get('relay_secret') or request.args.get('relay_secret', '')
    if token != RELAY_SECRET:
        return False
    return True


# ---------- PA session backup (persistent across Render restarts) ----------

def _push_session_to_pa(data):
    """Push session backup to PythonAnywhere for persistence."""
    if not PA_URL or RELAY_SECRET == 'change-me-in-production':
        return
    try:
        payload = json.dumps({
            'relay_secret': RELAY_SECRET,
            'action': 'save',
            'session_data': data,
        }).encode('utf-8')
        req = urllib.request.Request(
            PA_URL.rstrip('/') + '/api/relay-session-backup',
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        resp = urllib.request.urlopen(req, timeout=15)
        logging.info(f"Session pushed to PA backup: {resp.status}")
    except Exception as e:
        logging.warning(f"Failed to push session to PA: {e}")


def _pull_session_from_pa():
    """Pull session backup from PythonAnywhere."""
    if not PA_URL or RELAY_SECRET == 'change-me-in-production':
        return None
    try:
        payload = json.dumps({
            'relay_secret': RELAY_SECRET,
            'action': 'get',
        }).encode('utf-8')
        req = urllib.request.Request(
            PA_URL.rstrip('/') + '/api/relay-session-backup',
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        resp = urllib.request.urlopen(req, timeout=15)
        body = json.loads(resp.read().decode('utf-8'))
        sd = body.get('session_data')
        if sd and isinstance(sd, dict) and sd.get('session'):
            logging.info("Session restored from PA backup!")
            return sd
    except Exception as e:
        logging.warning(f"Failed to pull session from PA: {e}")
    return None


# ---------- Local session load / save ----------

def _load_session():
    """Load saved session data from file, falling back to PA backup."""
    global _pa_restore_attempted

    # 1. Try local file
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data.get('session'):
                    return data
    except Exception:
        pass

    # 2. Fallback: restore from PA backup (once per process)
    if not _pa_restore_attempted:
        _pa_restore_attempted = True
        pa_data = _pull_session_from_pa()
        if pa_data:
            # Save locally for further reads this process
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(SESSION_FILE, 'w', encoding='utf-8') as f:
                    json.dump(pa_data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            return pa_data

    # 3. Maybe local file has partial data (api_id/etc but no session)
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass

    return {}


def _save_session(data):
    """Save session data to local file AND push backup to PA."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SESSION_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"save_session error: {e}")

    # Async push to PA in background thread (don't block the request)
    if data.get('session'):
        threading.Thread(target=_push_session_to_pa, args=(data,), daemon=True).start()


def _run_async(coro):
    """Run an async coroutine from synchronous Flask code."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_client(session_str='', api_id=None, api_hash=None):
    """Create a TelegramClient."""
    data = _load_session()
    aid = api_id or int(data.get('api_id', 0))
    ahash = api_hash or data.get('api_hash', '')
    sess = session_str or data.get('session', '')
    if not aid or not ahash:
        return None
    return TelegramClient(
        StringSession(sess), aid, ahash,
        device_model='LunaGifts Relay',
        system_version='1.0',
        app_version='1.0',
    )


def _code_type_name(sent_type):
    """Convert Telegram SentCode type to human-readable name."""
    name = type(sent_type).__name__
    mapping = {
        'SentCodeTypeApp': 'Telegram (в приложении)',
        'SentCodeTypeSms': 'SMS',
        'SentCodeTypeCall': 'Звонок',
        'SentCodeTypeFlashCall': 'Flash-звонок',
        'SentCodeTypeMissedCall': 'Пропущенный звонок',
        'SentCodeTypeFragmentSms': 'Fragment SMS',
        'SentCodeTypeEmailCode': 'Email',
        'SentCodeTypeFirebaseSms': 'Firebase SMS',
    }
    return mapping.get(name, name)


# ---------- Keep-alive (prevents Render free tier spin-down) ----------

_start_time = time.time()
_keepalive_started = False

def _keepalive_worker():
    """Background thread: ping own public URL every 10 min to prevent Render spin-down."""
    ext_url = os.environ.get('RENDER_EXTERNAL_URL', '')
    if not ext_url:
        logging.info("RENDER_EXTERNAL_URL not set, keep-alive disabled")
        return
    logging.info(f"Keep-alive thread started, pinging {ext_url}/health every 10 min")
    while True:
        time.sleep(600)  # 10 minutes
        try:
            req = urllib.request.Request(ext_url + '/health', method='GET')
            urllib.request.urlopen(req, timeout=15)
            logging.debug("Keep-alive ping OK")
        except Exception as e:
            logging.debug(f"Keep-alive ping failed: {e}")


def _start_keepalive():
    global _keepalive_started
    if _keepalive_started:
        return
    _keepalive_started = True
    t = threading.Thread(target=_keepalive_worker, daemon=True)
    t.start()


# Start keep-alive when loaded by gunicorn
_start_keepalive()


# ---------- API Endpoints ----------

@app.route('/health')
def health():
    uptime = int(time.time() - _start_time)
    has_session = bool(_load_session().get('session'))
    return jsonify({
        'status': 'ok',
        'service': 'telethon-relay',
        'uptime_seconds': uptime,
        'session_present': has_session,
    })


@app.route('/status', methods=['POST'])
def status():
    if not _check_secret():
        return jsonify({'error': 'Unauthorized'}), 403

    data = _load_session()
    api_id = data.get('api_id', '')
    api_hash = data.get('api_hash', '')
    phone = data.get('phone', '')
    session_str = data.get('session', '')

    if not session_str or not api_id:
        return jsonify({
            'connected': False,
            'api_id': api_id,
            'api_hash': api_hash,
            'phone': phone,
        })

    # Check if session is valid + get balance
    async def _check():
        client = _make_client()
        if not client:
            return False, None, None
        try:
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                # Get star balance
                balance = None
                try:
                    stars_status = await client(tl_functions.payments.GetStarsStatusRequest(
                        peer=tl_types.InputPeerSelf()
                    ))
                    if hasattr(stars_status, 'balance'):
                        b = stars_status.balance
                        if hasattr(b, 'amount'):
                            balance = b.amount + (b.nanos / 1e9 if b.nanos else 0)
                        else:
                            balance = int(b)
                except Exception as e:
                    logging.warning(f"Balance check error: {e}")
                await client.disconnect()
                return True, me, balance
            await client.disconnect()
        except Exception as e:
            logging.warning(f"Session check error: {e}")
            try:
                await client.disconnect()
            except:
                pass
        return False, None, None

    connected, me, balance = _run_async(_check())
    result = {
        'connected': connected,
        'api_id': api_id,
        'api_hash': api_hash,
        'phone': phone,
    }
    if connected and me:
        name = (me.first_name or '') + (' ' + me.last_name if me.last_name else '')
        result['account_name'] = name.strip()
        result['account_id'] = me.id
        result['username'] = me.username or ''
        if balance is not None:
            result['star_balance'] = balance
    return jsonify(result)


@app.route('/send-code', methods=['POST'])
def send_code():
    if not _check_secret():
        return jsonify({'error': 'Unauthorized'}), 403

    body = request.json or {}
    api_id = str(body.get('api_id', '')).strip()
    api_hash = str(body.get('api_hash', '')).strip()
    phone = str(body.get('phone', '')).strip()

    if not api_id or not api_hash or not phone:
        return jsonify({'error': 'Заполните API ID, API Hash и телефон'}), 400

    try:
        api_id_int = int(api_id)
    except ValueError:
        return jsonify({'error': 'API ID должен быть числом'}), 400

    # Save credentials
    data = _load_session()
    data['api_id'] = api_id
    data['api_hash'] = api_hash
    data['phone'] = phone
    _save_session(data)

    # force_sms=True → resend as SMS if previous code was in-app
    force_sms = body.get('force_sms', False)

    async def _send():
        client = _make_client('', api_id_int, api_hash)
        await client.connect()

        if force_sms:
            # Try to resend code via SMS
            old_data = _load_session()
            old_hash = old_data.get('phone_code_hash', '')
            old_session = old_data.get('temp_session', '')
            if old_hash and old_session:
                # Reconnect with the temp session that started auth
                client2 = TelegramClient(StringSession(old_session), api_id_int, api_hash)
                await client2.connect()
                try:
                    from telethon.tl.functions.auth import ResendCodeRequest
                    result = await client2(ResendCodeRequest(phone, old_hash))
                    ts = client2.session.save()
                    await client2.disconnect()
                    await client.disconnect()
                    return result.phone_code_hash, ts, _code_type_name(result.type)
                except Exception as e:
                    logging.warning(f"ResendCode failed: {e}, falling back to new send_code")
                    await client2.disconnect()

        result = await client.send_code_request(phone)
        temp_session = client.session.save()
        await client.disconnect()
        return result.phone_code_hash, temp_session, _code_type_name(result.type)

    try:
        pch, temp_session, code_type = _run_async(_send())
        data = _load_session()
        data['phone_code_hash'] = pch
        data['temp_session'] = temp_session
        _save_session(data)
        logging.info(f"Code sent via: {code_type}, phone_code_hash: {pch[:8]}...")
        msg = f'Код отправлен через {code_type}'
        return jsonify({'success': True, 'message': msg, 'code_type': code_type})
    except Exception as e:
        err = str(e)
        logging.error(f"send_code error: {err}")
        return jsonify({'error': f'Ошибка: {err}'}), 400


@app.route('/sign-in', methods=['POST'])
def sign_in():
    if not _check_secret():
        return jsonify({'error': 'Unauthorized'}), 403

    body = request.json or {}
    code = str(body.get('code', '')).strip()
    password = str(body.get('password', '')).strip()

    data = _load_session()
    temp_session = data.get('temp_session', '')
    phone = data.get('phone', '')
    pch = data.get('phone_code_hash', '')
    api_id = int(data.get('api_id', 0))
    api_hash = data.get('api_hash', '')

    if not temp_session or not pch:
        return jsonify({'error': 'Сначала отправьте код'}), 400

    async def _sign():
        client = TelegramClient(StringSession(temp_session), api_id, api_hash)
        await client.connect()

        if code and not password:
            try:
                await client.sign_in(phone, code, phone_code_hash=pch)
            except tl_errors.SessionPasswordNeededError:
                updated = client.session.save()
                await client.disconnect()
                return {'need_2fa': True, 'updated_session': updated}
            except Exception as e:
                await client.disconnect()
                return {'error': str(e)}
        elif password:
            try:
                await client.sign_in(password=password)
            except Exception as e:
                await client.disconnect()
                return {'error': str(e)}
        else:
            await client.disconnect()
            return {'error': 'Введите код или пароль'}

        if await client.is_user_authorized():
            me = await client.get_me()
            sess_str = client.session.save()
            await client.disconnect()
            return {
                'success': True,
                'session': sess_str,
                'name': (me.first_name or '') + (' ' + me.last_name if me.last_name else ''),
                'user_id': me.id,
                'username': me.username or '',
            }
        await client.disconnect()
        return {'error': 'Авторизация не удалась'}

    try:
        result = _run_async(_sign())
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    if result.get('error'):
        return jsonify({'error': result['error']}), 400

    if result.get('need_2fa'):
        data = _load_session()
        data['temp_session'] = result['updated_session']
        _save_session(data)
        return jsonify({'need_2fa': True, 'message': 'Требуется пароль 2FA'})

    if result.get('success'):
        data = _load_session()
        data['session'] = result['session']
        data.pop('temp_session', None)
        data.pop('phone_code_hash', None)
        _save_session(data)
        return jsonify({
            'success': True,
            'account_name': result['name'],
            'account_id': result['user_id'],
            'username': result['username'],
        })

    return jsonify({'error': 'Неизвестная ошибка'}), 400


@app.route('/import-session', methods=['POST'])
def import_session():
    """Import a pre-generated StringSession (bypasses send-code/sign-in)."""
    if not _check_secret():
        return jsonify({'error': 'Unauthorized'}), 403

    body = request.json or {}
    session_str = str(body.get('session_string', '')).strip()
    api_id = str(body.get('api_id', '')).strip()
    api_hash = str(body.get('api_hash', '')).strip()

    if not session_str:
        return jsonify({'error': 'session_string обязателен'}), 400
    if not api_id or not api_hash:
        return jsonify({'error': 'api_id и api_hash обязательны'}), 400

    try:
        api_id_int = int(api_id)
    except ValueError:
        return jsonify({'error': 'API ID должен быть числом'}), 400

    # Validate the session by connecting
    async def _validate():
        client = TelegramClient(StringSession(session_str), api_id_int, api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                return None, 'Сессия невалидна или истекла'
            me = await client.get_me()
            saved = client.session.save()
            await client.disconnect()
            name = (me.first_name or '') + (' ' + me.last_name if me.last_name else '')
            return {
                'success': True,
                'session': saved,
                'name': name.strip(),
                'user_id': me.id,
                'username': me.username or '',
                'phone': me.phone or '',
            }, None
        except Exception as e:
            try:
                await client.disconnect()
            except:
                pass
            return None, str(e)

    try:
        result, err = _run_async(_validate())
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    if err:
        return jsonify({'error': err}), 400

    # Save session
    data = _load_session()
    data['session'] = result['session']
    data['api_id'] = api_id
    data['api_hash'] = api_hash
    data['phone'] = result.get('phone', '')
    data.pop('temp_session', None)
    data.pop('phone_code_hash', None)
    _save_session(data)

    return jsonify({
        'success': True,
        'account_name': result['name'],
        'account_id': result['user_id'],
        'username': result['username'],
    })


# ---------- QR Login ----------

@app.route('/qr-login/start', methods=['POST'])
def qr_login_start():
    """Generate QR login token."""
    if not _check_secret():
        return jsonify({'error': 'Unauthorized'}), 403

    body = request.json or {}
    api_id = str(body.get('api_id', '')).strip()
    api_hash = str(body.get('api_hash', '')).strip()

    if not api_id or not api_hash:
        return jsonify({'error': 'api_id и api_hash обязательны'}), 400

    try:
        api_id_int = int(api_id)
    except ValueError:
        return jsonify({'error': 'API ID должен быть числом'}), 400

    async def _start():
        client = TelegramClient(StringSession(), api_id_int, api_hash,
            device_model='LunaGifts Web', system_version='1.0', app_version='1.0')
        await client.connect()
        result = await client(ExportLoginTokenRequest(
            api_id=api_id_int, api_hash=api_hash, except_ids=[]
        ))
        if isinstance(result, LoginToken):
            token_b64 = base64.urlsafe_b64encode(result.token).decode()
            temp_session = client.session.save()
            await client.disconnect()
            return {
                'success': True,
                'token': token_b64,
                'expires': result.expires.timestamp(),
                'temp_session': temp_session,
            }
        await client.disconnect()
        return {'error': f'Unexpected result: {type(result).__name__}'}

    try:
        result = _run_async(_start())
        if result.get('error'):
            return jsonify({'error': result['error']}), 400

        # Save temp session for checking later
        data = _load_session()
        data['qr_temp_session'] = result['temp_session']
        data['api_id'] = api_id
        data['api_hash'] = api_hash
        _save_session(data)

        return jsonify(result)
    except Exception as e:
        logging.error(f'qr-login/start error: {e}')
        return jsonify({'error': str(e)}), 400


@app.route('/qr-login/check', methods=['POST'])
def qr_login_check():
    """Check if QR was scanned, return new token if expired."""
    if not _check_secret():
        return jsonify({'error': 'Unauthorized'}), 403

    data = _load_session()
    temp_session = data.get('qr_temp_session', '')
    api_id = int(data.get('api_id', 0))
    api_hash = data.get('api_hash', '')

    if not temp_session or not api_id:
        return jsonify({'error': 'Сначала запустите QR вход'}), 400

    async def _check():
        client = TelegramClient(StringSession(temp_session), api_id, api_hash,
            device_model='LunaGifts Web', system_version='1.0', app_version='1.0')
        await client.connect()
        try:
            result = await client(ExportLoginTokenRequest(
                api_id=api_id, api_hash=api_hash, except_ids=[]
            ))
            if isinstance(result, LoginTokenSuccess):
                if await client.is_user_authorized():
                    me = await client.get_me()
                    sess = client.session.save()
                    await client.disconnect()
                    name = (me.first_name or '') + (' ' + me.last_name if me.last_name else '')
                    return {'success': True, 'session': sess,
                            'name': name.strip(), 'user_id': me.id,
                            'username': me.username or '', 'phone': me.phone or ''}
                await client.disconnect()
                return {'error': 'Авторизация не завершена'}
            elif isinstance(result, LoginTokenMigrateTo):
                await client._switch_dc(result.dc_id)
                result2 = await client(ImportLoginTokenRequest(result.token))
                if isinstance(result2, LoginTokenSuccess):
                    if await client.is_user_authorized():
                        me = await client.get_me()
                        sess = client.session.save()
                        await client.disconnect()
                        name = (me.first_name or '') + (' ' + me.last_name if me.last_name else '')
                        return {'success': True, 'session': sess,
                                'name': name.strip(), 'user_id': me.id,
                                'username': me.username or '', 'phone': me.phone or ''}
                await client.disconnect()
                return {'error': 'Миграция DC не удалась'}
            elif isinstance(result, LoginToken):
                # Not scanned yet, return fresh token
                token_b64 = base64.urlsafe_b64encode(result.token).decode()
                updated = client.session.save()
                await client.disconnect()
                return {'waiting': True, 'token': token_b64, 'temp_session': updated}
            await client.disconnect()
            return {'waiting': True}
        except tl_errors.SessionPasswordNeededError:
            updated = client.session.save()
            await client.disconnect()
            return {'need_2fa': True, 'temp_session': updated}
        except Exception as e:
            try:
                await client.disconnect()
            except:
                pass
            return {'error': str(e)}

    try:
        result = _run_async(_check())
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    if result.get('error'):
        return jsonify({'error': result['error']}), 400

    # Update temp session if changed
    if result.get('temp_session'):
        data = _load_session()
        data['qr_temp_session'] = result['temp_session']
        _save_session(data)

    if result.get('need_2fa'):
        return jsonify({'need_2fa': True})

    if result.get('success'):
        # Save the real session
        data = _load_session()
        data['session'] = result['session']
        data['phone'] = result.get('phone', '')
        data.pop('qr_temp_session', None)
        _save_session(data)
        return jsonify({
            'success': True,
            'account_name': result['name'],
            'account_id': result['user_id'],
            'username': result['username'],
        })

    # Still waiting
    return jsonify({'waiting': True, 'token': result.get('token', '')})


@app.route('/qr-login/2fa', methods=['POST'])
def qr_login_2fa():
    """Complete QR login with 2FA password."""
    if not _check_secret():
        return jsonify({'error': 'Unauthorized'}), 403

    body = request.json or {}
    password = str(body.get('password', '')).strip()
    if not password:
        return jsonify({'error': 'Введите пароль'}), 400

    data = _load_session()
    temp_session = data.get('qr_temp_session', '')
    api_id = int(data.get('api_id', 0))
    api_hash = data.get('api_hash', '')

    if not temp_session:
        return jsonify({'error': 'Нет активной QR сессии'}), 400

    async def _2fa():
        client = TelegramClient(StringSession(temp_session), api_id, api_hash)
        await client.connect()
        try:
            await client.sign_in(password=password)
            if await client.is_user_authorized():
                me = await client.get_me()
                sess = client.session.save()
                await client.disconnect()
                name = (me.first_name or '') + (' ' + me.last_name if me.last_name else '')
                return {'success': True, 'session': sess,
                        'name': name.strip(), 'user_id': me.id,
                        'username': me.username or '', 'phone': me.phone or ''}
            await client.disconnect()
            return {'error': 'Авторизация не завершена'}
        except tl_errors.PasswordHashInvalidError:
            await client.disconnect()
            return {'error': 'Неверный пароль'}
        except Exception as e:
            try:
                await client.disconnect()
            except:
                pass
            return {'error': str(e)}

    try:
        result = _run_async(_2fa())
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    if result.get('error'):
        return jsonify({'error': result['error']}), 400

    if result.get('success'):
        data = _load_session()
        data['session'] = result['session']
        data['phone'] = result.get('phone', '')
        data.pop('qr_temp_session', None)
        _save_session(data)
        return jsonify({
            'success': True,
            'account_name': result['name'],
            'account_id': result['user_id'],
            'username': result['username'],
        })

    return jsonify({'error': 'Неизвестная ошибка'}), 400


@app.route('/disconnect', methods=['POST'])
def disconnect():
    if not _check_secret():
        return jsonify({'error': 'Unauthorized'}), 403

    data = _load_session()
    data.pop('session', None)
    data.pop('temp_session', None)
    data.pop('phone_code_hash', None)
    _save_session(data)
    return jsonify({'success': True})


@app.route('/get-star-gifts', methods=['POST'])
def get_star_gifts():
    """Fetch available star gifts from Telegram catalog with sticker thumbnails."""
    if not _check_secret():
        return jsonify({'error': 'Unauthorized'}), 403

    data = _load_session()
    if not data.get('session'):
        return jsonify({'ok': False, 'error': 'Сессия не настроена'}), 400

    include_thumbs = (request.json or {}).get('include_thumbs', False)

    async def _fetch():
        client = _make_client()
        if not client:
            return None, 'Клиент не создан'
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return None, 'Сессия истекла'

            # Also fetch balance
            balance = None
            try:
                stars_status = await client(tl_functions.payments.GetStarsStatusRequest(
                    peer=tl_types.InputPeerSelf()
                ))
                if hasattr(stars_status, 'balance'):
                    b = stars_status.balance
                    if hasattr(b, 'amount'):
                        balance = b.amount + (b.nanos / 1e9 if b.nanos else 0)
                    else:
                        balance = int(b)
            except Exception as be:
                logging.warning(f"Balance check in catalog: {be}")

            result = await client(tl_functions.payments.GetStarGiftsRequest(hash=0))
            gifts = []
            gift_list = getattr(result, 'gifts', [])
            for g in gift_list:
                gift_data = {
                    'id': g.id,
                    'stars': g.stars,
                    'convert_stars': getattr(g, 'convert_stars', 0),
                    'limited': getattr(g, 'limited', False),
                    'sold_out': getattr(g, 'sold_out', False),
                    'availability_remains': getattr(g, 'availability_remains', None),
                    'availability_total': getattr(g, 'availability_total', None),
                    'title': getattr(g, 'title', ''),
                }
                # Try to get sticker thumbnail as base64
                if include_thumbs:
                    sticker = getattr(g, 'sticker', None)
                    if sticker:
                        try:
                            thumb_bytes = await client.download_media(sticker, bytes, thumb=0)
                            if thumb_bytes:
                                gift_data['thumb_b64'] = base64.b64encode(thumb_bytes).decode('ascii')
                                gift_data['thumb_mime'] = 'image/webp'
                        except Exception as te:
                            logging.debug(f"Thumb download failed for gift {g.id}: {te}")
                gifts.append(gift_data)
            await client.disconnect()
            return gifts, balance, ''
        except Exception as e:
            try:
                await client.disconnect()
            except:
                pass
            return None, None, str(e)

    try:
        result = _run_async(_fetch())
        gifts, balance, err = result
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        resp = {'ok': True, 'gifts': gifts}
        if balance is not None:
            resp['star_balance'] = balance
        return jsonify(resp)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/send-gift', methods=['POST'])
def send_gift():
    if not _check_secret():
        return jsonify({'error': 'Unauthorized'}), 403

    body = request.json or {}
    user_id = body.get('user_id')
    gift_id = body.get('gift_id')

    if not user_id or not gift_id:
        return jsonify({'error': 'user_id and gift_id required'}), 400

    data = _load_session()
    if not data.get('session'):
        return jsonify({'ok': False, 'error': 'Сессия не настроена'}), 400

    async def _send():
        client = _make_client()
        if not client:
            return False, 'Клиент не создан'
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return False, 'Сессия истекла, переавторизуйтесь'

            # Check star balance before sending
            try:
                stars_status = await client(tl_functions.payments.GetStarsStatusRequest(
                    peer=tl_types.InputPeerSelf()
                ))
                balance = 0
                if hasattr(stars_status, 'balance'):
                    b = stars_status.balance
                    if hasattr(b, 'amount'):
                        balance = b.amount
                    else:
                        balance = int(b)
                logging.info(f"Account star balance: {balance}")
            except Exception as be:
                logging.warning(f"Balance check failed: {be}")
                balance = None

            target = await client.get_input_entity(int(user_id))
            logging.info(f"Sending star gift {gift_id} to user {user_id}, peer={target}")

            # Validate gift exists in current catalog
            try:
                catalog = await client(tl_functions.payments.GetStarGiftsRequest(hash=0))
                gift_list = getattr(catalog, 'gifts', [])
                valid_ids = {g.id for g in gift_list}
                gid = int(gift_id)
                if gid not in valid_ids:
                    # Try to find closest match by star price
                    avail = [{'id': g.id, 'stars': g.stars, 'title': getattr(g, 'title', ''), 'sold_out': getattr(g, 'sold_out', False)} for g in gift_list if not getattr(g, 'sold_out', False)]
                    avail_str = ', '.join(f"{a['id']}({a['stars']}⭐)" for a in avail[:20])
                    logging.error(f"Gift {gift_id} not found in catalog. Available: {avail_str}")
                    await client.disconnect()
                    return False, f'STARGIFT_INVALID: подарок {gift_id} не найден в каталоге Telegram. Доступные ID: {avail_str}'
                logging.info(f"Gift {gift_id} found in catalog, proceeding to send")
            except Exception as ce:
                logging.warning(f"Catalog validation failed (proceeding anyway): {ce}")

            invoice = tl_types.InputInvoiceStarGift(
                peer=target,
                gift_id=int(gift_id),
                message=tl_types.TextWithEntities(
                    text='\u2b50 Gift from @lunagifts_robot\nPromocode from 3 stars \u201cLunaVPN\u201d',
                    entities=[
                        tl_types.MessageEntityCustomEmoji(
                            offset=0,
                            length=1,
                            document_id=5456140674028019486
                        )
                    ]
                ),
            )

            form = await client(tl_functions.payments.GetPaymentFormRequest(
                invoice=invoice,
            ))
            logging.info(f"Got payment form: form_id={form.form_id}")

            result = await client(tl_functions.payments.SendStarsFormRequest(
                form_id=form.form_id,
                invoice=invoice,
            ))
            logging.info(f"SendStarsForm result: {result}")

            await client.disconnect()
            return True, ''
        except Exception as e:
            try:
                await client.disconnect()
            except:
                pass
            logging.error(f"send_gift exception: {type(e).__name__}: {e}")
            return False, str(e)

    try:
        ok, err = _run_async(_send())
        if ok:
            return jsonify({'ok': True})
        else:
            logging.error(f"send_gift error user={user_id} gift={gift_id}: {err}")
            return jsonify({'ok': False, 'error': err}), 400
    except Exception as e:
        logging.error(f"send_gift exception: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 400


if __name__ == '__main__':
    _start_keepalive()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
