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

# Persistent session data file
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
SESSION_FILE = os.path.join(DATA_DIR, 'session.json')

# Temp auth state (in-memory, per-process)
_auth_state = {}


def _check_secret():
    """Validate relay secret from request."""
    token = (request.json or {}).get('relay_secret') or request.args.get('relay_secret', '')
    if token != RELAY_SECRET:
        return False
    return True


def _load_session():
    """Load saved session data from file."""
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_session(data):
    """Save session data to file."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SESSION_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"save_session error: {e}")


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


# ---------- API Endpoints ----------

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'telethon-relay'})


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

    # Check if session is valid
    async def _check():
        client = _make_client()
        if not client:
            return False, None
        try:
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                await client.disconnect()
                return True, me
            await client.disconnect()
        except Exception as e:
            logging.warning(f"Session check error: {e}")
            try:
                await client.disconnect()
            except:
                pass
        return False, None

    connected, me = _run_async(_check())
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
    """Fetch available star gifts from Telegram catalog."""
    if not _check_secret():
        return jsonify({'error': 'Unauthorized'}), 403

    data = _load_session()
    if not data.get('session'):
        return jsonify({'ok': False, 'error': 'Сессия не настроена'}), 400

    async def _fetch():
        client = _make_client()
        if not client:
            return None, 'Клиент не создан'
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return None, 'Сессия истекла'

            result = await client(tl_functions.payments.GetStarGiftsRequest(hash=0))
            gifts = []
            gift_list = getattr(result, 'gifts', [])
            for g in gift_list:
                gifts.append({
                    'id': g.id,
                    'stars': g.stars,
                    'convert_stars': getattr(g, 'convert_stars', 0),
                    'limited': getattr(g, 'limited', False),
                    'sold_out': getattr(g, 'sold_out', False),
                    'availability_remains': getattr(g, 'availability_remains', None),
                    'availability_total': getattr(g, 'availability_total', None),
                    'title': getattr(g, 'title', ''),
                })
            await client.disconnect()
            return gifts, ''
        except Exception as e:
            try:
                await client.disconnect()
            except:
                pass
            return None, str(e)

    try:
        gifts, err = _run_async(_fetch())
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        return jsonify({'ok': True, 'gifts': gifts})
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

            target = await client.get_input_entity(int(user_id))
            logging.info(f"Sending star gift {gift_id} to user {user_id}, peer={target}")

            invoice = tl_types.InputInvoiceStarGift(
                peer=target,
                gift_id=int(gift_id),
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
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
