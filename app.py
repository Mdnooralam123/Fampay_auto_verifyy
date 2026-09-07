"""
KHAN PAY – Ultra-Fast UPI Payment Verifier
Complete error handling, safe Supabase/Gmail fallback, real polling (2s).
QR generated server-side for reliability and scannability.
"""

import os
import re
import time
import json
import logging
import sys
from io import BytesIO
from datetime import datetime, timedelta, timezone

from flask import Flask, request, jsonify, send_file, render_template_string, redirect, url_for
from flask_cors import CORS

# Optional imports with safe fallback
try:
    import imaplib
    import email
    from email.header import decode_header
except ImportError:
    imaplib = None
    email = None
    decode_header = None

try:
    import secrets
except ImportError:
    secrets = None

try:
    import qrcode
except ImportError:
    qrcode = None

# ============================================
# CONFIG (read from environment, fallback defaults)
# ============================================
CONFIG = {
    'UPI_ID': os.getenv('UPI_ID', '9304619487@fam'),
    'PAYEE_NAME': os.getenv('PAYEE_NAME', 'KHAN PAY'),
    'GMAIL_APP_PASSWORD': os.getenv('GMAIL_APP_PASSWORD', 'owjwtlotkfjnsftm'),
    'GMAIL_EMAIL': os.getenv('GMAIL_EMAIL', 'nkg166465@gmail.com'),
    'TIME_WINDOW_MINUTES': int(os.getenv('TIME_WINDOW_MINUTES', 5)),
    'ADMIN_API_KEY': os.getenv('ADMIN_API_KEY', 'admin_1234567890'),
    'MAX_EMAILS_CHECK': int(os.getenv('MAX_EMAILS_CHECK', 50)),
    'SUPABASE_URL': os.getenv('SUPABASE_URL'),
    'SUPABASE_KEY': os.getenv('SUPABASE_KEY'),
}

# ============================================
# LOGGING (write to stderr for Vercel logs)
# ============================================
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# ============================================
# FLASK APP
# ============================================
app = Flask(__name__)
CORS(app)

# ============================================
# GLOBAL EXCEPTION HANDLER (returns JSON for API, HTML for pages)
# ============================================
@app.errorhandler(Exception)
def handle_exception(e):
    logger.error(f"Unhandled exception: {e}", exc_info=True)
    if request.path.startswith('/api/') or request.path.startswith('/verify-') or request.path.startswith('/admin_'):
        return jsonify({
            'status': 'error',
            'message': 'Internal server error',
            'detail': str(e) if app.debug else None
        }), 500
    return render_template_string('''
        <!DOCTYPE html>
        <html>
        <head><title>Error</title>
        <style>body{font-family:sans-serif;text-align:center;padding:2rem;background:#f8fafc;color:#1a202c;}</style>
        </head>
        <body>
        <h1>⚠️ Something went wrong</h1>
        <p>Please try again later.</p>
        <p><a href="/" style="color:#4a6cf7;">Go Home</a></p>
        </body>
        </html>
    ''', 500)

# ============================================
# SUPABASE CLIENT (optional, with safe fallback)
# ============================================
supabase_client = None
try:
    from supabase import create_client
    if CONFIG['SUPABASE_URL'] and CONFIG['SUPABASE_KEY']:
        supabase_client = create_client(CONFIG['SUPABASE_URL'], CONFIG['SUPABASE_KEY'])
        logger.info("Supabase client initialized.")
    else:
        logger.warning("Supabase credentials missing; using in‑memory fallback.")
except ImportError:
    logger.warning("supabase package not installed; using in‑memory fallback.")
except Exception as e:
    logger.error(f"Supabase init error: {e}; using in‑memory fallback.")

# ============================================
# FALLBACK STORAGE (in‑memory)
# ============================================
app.fallback_orders = {}
app.fallback_api_keys = {}
app.fallback_utrs = {}

# ============================================
# HELPER: timezone‑aware IST
# ============================================
IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(IST)

def format_ist(dt):
    return dt.strftime('%d-%m-%Y %H:%M:%S')

# ============================================
# DATABASE HELPERS (Supabase + fallback)
# ============================================
def db_create_order(api_key, amount):
    try:
        order_id = f"Khan_{secrets.token_hex(4).upper()}" if secrets else f"Khan_{int(time.time())}"
    except:
        order_id = f"Khan_{int(time.time())}"
    now_utc = datetime.now(timezone.utc)
    now_ist_dt = now_utc.astimezone(IST)
    expires_utc = now_utc + timedelta(minutes=CONFIG['TIME_WINDOW_MINUTES'])
    expires_ist_dt = expires_utc.astimezone(IST)
    created_at_ist = format_ist(now_ist_dt)
    expires_at_ist = format_ist(expires_ist_dt)
    data = {
        'order_id': order_id,
        'api_key': api_key,
        'amount': amount,
        'payable_amount': amount,
        'status': 'pending',
        'created_at': created_at_ist,
        'expires_at': expires_at_ist,
        'utr': None,
        'transaction_id': None,
        'sender_name': None,
        'payment_time': None,
        'verified_at': None
    }
    if supabase_client:
        try:
            supabase_client.table('orders').insert(data).execute()
            logger.info(f"Order {order_id} created in Supabase.")
        except Exception as e:
            logger.error(f"Supabase insert error: {e}")
            app.fallback_orders[order_id] = data
    else:
        app.fallback_orders[order_id] = data
    return order_id

def db_get_order(order_id):
    if supabase_client:
        try:
            result = supabase_client.table('orders').select('*').eq('order_id', order_id).execute()
            if result.data:
                return result.data[0]
        except Exception as e:
            logger.error(f"Supabase get error: {e}")
    return app.fallback_orders.get(order_id)

def db_update_order(order_id, **kwargs):
    if supabase_client:
        try:
            supabase_client.table('orders').update(kwargs).eq('order_id', order_id).execute()
        except Exception as e:
            logger.error(f"Supabase update error: {e}")
            if order_id in app.fallback_orders:
                app.fallback_orders[order_id].update(kwargs)
    else:
        if order_id in app.fallback_orders:
            app.fallback_orders[order_id].update(kwargs)

def db_create_api_key(name, expiry_hours=24):
    try:
        api_key = f"fam_{secrets.token_hex(20)}" if secrets else f"fam_{int(time.time())}"
    except:
        api_key = f"fam_{int(time.time())}"
    now_utc = datetime.now(timezone.utc)
    expires_utc = now_utc + timedelta(hours=expiry_hours)
    data = {
        'api_key': api_key,
        'name': name,
        'created_at': now_utc.isoformat(),
        'expires_at': expires_utc.isoformat(),
        'is_active': 1
    }
    if supabase_client:
        try:
            supabase_client.table('api_keys').insert(data).execute()
        except Exception as e:
            logger.error(f"Supabase insert api_key error: {e}")
            app.fallback_api_keys[api_key] = data
    else:
        app.fallback_api_keys[api_key] = data
    return api_key

def db_validate_api_key(api_key):
    """
    Validate API key: check existence, active status, and expiry.
    Uses proper datetime comparison to avoid timezone/string issues.
    """
    if supabase_client:
        try:
            result = supabase_client.table('api_keys').select('*').eq('api_key', api_key).eq('is_active', 1).execute()
            if result.data:
                key = result.data[0]
                expires_at = datetime.fromisoformat(key['expires_at'])
                if datetime.now(timezone.utc) < expires_at:
                    return key
            return None
        except Exception as e:
            logger.error(f"Supabase validate error: {e}")
    # Fallback storage
    key = app.fallback_api_keys.get(api_key)
    if key and key['is_active'] == 1:
        try:
            expires_at = datetime.fromisoformat(key['expires_at'])
            if datetime.now(timezone.utc) < expires_at:
                return key
        except:
            return None
    return None

def db_is_utr_verified(utr):
    if supabase_client:
        try:
            result = supabase_client.table('verified_utrs').select('*').eq('utr', utr).execute()
            return len(result.data) > 0
        except Exception as e:
            logger.error(f"Supabase verify utr error: {e}")
    return utr in app.fallback_utrs

def db_mark_utr_verified(utr, order_id):
    data = {'utr': utr, 'order_id': order_id, 'verified_at': datetime.now(timezone.utc).isoformat()}
    if supabase_client:
        try:
            supabase_client.table('verified_utrs').insert(data).execute()
        except Exception as e:
            logger.error(f"Supabase mark utr error: {e}")
            app.fallback_utrs[utr] = data
    else:
        app.fallback_utrs[utr] = data

def db_get_pending_orders():
    if supabase_client:
        try:
            result = supabase_client.table('orders').select('*').eq('status', 'pending').execute()
            return result.data
        except Exception as e:
            logger.error(f"Supabase get pending error: {e}")
    return [o for o in app.fallback_orders.values() if o['status'] == 'pending']

def db_update_api_key(api_key, **kwargs):
    if supabase_client:
        try:
            supabase_client.table('api_keys').update(kwargs).eq('api_key', api_key).execute()
        except Exception as e:
            logger.error(f"Supabase update api_key error: {e}")
            if api_key in app.fallback_api_keys:
                app.fallback_api_keys[api_key].update(kwargs)
    else:
        if api_key in app.fallback_api_keys:
            app.fallback_api_keys[api_key].update(kwargs)

# ============================================
# GMAIL VERIFICATION (on-demand, with safe fallback)
# ============================================
def connect_imap():
    if not CONFIG['GMAIL_EMAIL'] or not CONFIG['GMAIL_APP_PASSWORD']:
        raise Exception("Gmail credentials not configured")
    if imaplib is None:
        raise Exception("IMAP library not available")
    mail = imaplib.IMAP4_SSL('imap.gmail.com')
    mail.login(CONFIG['GMAIL_EMAIL'], CONFIG['GMAIL_APP_PASSWORD'])
    mail.select('INBOX')
    return mail

def get_email_body(mail, msg_id):
    try:
        result, data = mail.fetch(msg_id, '(RFC822)')
        if result != 'OK':
            return ''
        raw = data[0][1]
        msg = email.message_from_bytes(raw)
        body = ''
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == 'text/plain' and 'attachment' not in str(part.get('Content-Disposition')):
                    try:
                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        break
                    except:
                        continue
        else:
            try:
                body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
            except:
                pass
        return body
    except:
        return ''

def parse_payment_email(body):
    details = {'amount': None, 'utr': None, 'transaction_id': None, 'sender': None,
               'date': None, 'type': None, 'payment_datetime': None, 'time_diff_minutes': None}
    if 'successfully received' in body.lower():
        details['type'] = 'received'
    elif 'successfully paid' in body.lower():
        details['type'] = 'paid'
        return details
    patterns = [r'₹([0-9]+(\.[0-9]+)?)', r'Amount\s*[:]\s*₹([0-9]+(\.[0-9]+)?)']
    for p in patterns:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            details['amount'] = float(m.group(1))
            break
    utr_patterns = [r'UTR\s*[:]\s*([0-9]+)', r'UTR\s*([0-9]+)']
    for p in utr_patterns:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            details['utr'] = m.group(1)
            break
    tx_patterns = [r'Transaction ID\s*[:]\s*([A-Z0-9]+)', r'Txn\s*[:]\s*([A-Z0-9]+)']
    for p in tx_patterns:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            details['transaction_id'] = m.group(1)
            break
    sender_match = re.search(r'from\s*([A-Za-z\s.]+)', body, re.IGNORECASE)
    if sender_match:
        details['sender'] = sender_match.group(1).strip()
    date_match = re.search(r'([0-9]{2}:[0-9]{2}\s*(AM|PM)\s*IST,\s*[0-9]{2}\s*[A-Za-z]+\s*[0-9]{4})', body, re.IGNORECASE)
    if date_match:
        details['date'] = date_match.group(1)
        try:
            time_str = date_match.group(1)
            time_part = re.search(r'([0-9]{2}:[0-9]{2})\s*(AM|PM)', time_str)
            if time_part:
                hour, minute = map(int, time_part.group(1).split(':'))
                ampm = time_part.group(2)
                if ampm == 'PM' and hour != 12:
                    hour += 12
                elif ampm == 'AM' and hour == 12:
                    hour = 0
                now_utc = datetime.now(timezone.utc)
                now_ist = now_utc.astimezone(IST)
                dt = datetime(now_ist.year, now_ist.month, now_ist.day, hour, minute, tzinfo=IST)
                if dt > now_ist:
                    dt -= timedelta(days=1)
                details['payment_datetime'] = dt.isoformat()
                details['time_diff_minutes'] = round((now_ist - dt).total_seconds() / 60, 1)
        except:
            pass
    return details

def search_gmail_payment(amount=None, utr=None, time_window=None):
    if time_window is None:
        time_window = CONFIG['TIME_WINDOW_MINUTES']
    mail = None
    try:
        mail = connect_imap()
        result, data = mail.search(None, 'ALL')
        if result != 'OK' or not data[0]:
            return None
        ids = data[0].split()
        recent_ids = ids[-CONFIG['MAX_EMAILS_CHECK']:]
        now_ist = datetime.now(IST)
        for msg_id in recent_ids:
            msg_id_str = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
            body = get_email_body(mail, msg_id_str)
            if not body:
                continue
            details = parse_payment_email(body)
            if details.get('type') != 'received':
                continue
            if details.get('payment_datetime'):
                dt = datetime.fromisoformat(details['payment_datetime'])
                if (now_ist - dt).total_seconds() / 60 > time_window:
                    continue
            elif details.get('time_diff_minutes') is not None and details['time_diff_minutes'] > time_window:
                continue
            else:
                try:
                    result2, data2 = mail.fetch(msg_id, '(BODY.PEEK[HEADER.FIELDS (DATE)])')
                    if result2 == 'OK':
                        header = data2[0][1].decode('utf-8', errors='ignore')
                        date_match = re.search(r'Date:\s*(.+)', header, re.IGNORECASE)
                        if date_match:
                            email_date = email.utils.parsedate_to_datetime(date_match.group(1))
                            if email_date.tzinfo is None:
                                email_date = email_date.replace(tzinfo=timezone.utc)
                            diff = (datetime.now(timezone.utc) - email_date).total_seconds() / 60
                            if diff > time_window:
                                continue
                except:
                    pass
            if amount is not None and details.get('amount') and abs(details['amount'] - amount) < 0.01:
                if utr is not None:
                    if details.get('utr') == utr:
                        return details
                else:
                    return details
            elif utr is not None and details.get('utr') == utr:
                return details
        return None
    except Exception as e:
        logger.error(f"Gmail search error: {e}")
        return None
    finally:
        if mail:
            try:
                mail.close()
                mail.logout()
            except:
                pass

# ============================================
# ADMIN ROUTES
# ============================================
ADMIN_KEY = CONFIG['ADMIN_API_KEY']

def admin_required():
    provided = request.args.get('admin_key')
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith('Bearer '):
        provided = auth_header.split(' ')[1]
    return provided == ADMIN_KEY

@app.route('/apikey_generate', methods=['GET'])
def apikey_generate():
    try:
        if not admin_required():
            return jsonify({'status': 'error', 'message': 'Invalid or missing admin_key'}), 401
        name = request.args.get('name')
        if not name:
            return jsonify({'status': 'error', 'message': 'name parameter required'}), 400
        hours = request.args.get('hours')
        days = request.args.get('days')
        expiry_hours = 24
        if hours:
            try: expiry_hours = int(hours)
            except: pass
        elif days:
            try: expiry_hours = int(days) * 24
            except: pass
        api_key = db_create_api_key(name, expiry_hours)
        now_utc = datetime.now(timezone.utc)
        expiry_utc = now_utc + timedelta(hours=expiry_hours)
        expiry_ist = expiry_utc.astimezone(IST)
        return jsonify({
            'status': 'success',
            'api_key': api_key,
            'name': name,
            'expires_at': expiry_utc.isoformat(),
            'expires_at_ist': format_ist(expiry_ist),
            'expires_in_hours': expiry_hours
        })
    except Exception as e:
        logger.error(f"apikey_generate error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/admin_orders', methods=['GET'])
def admin_orders():
    try:
        if not admin_required():
            return jsonify({'status': 'error', 'message': 'Invalid or missing admin_key'}), 401
        orders = db_get_pending_orders()
        return jsonify({'status': 'success', 'orders': orders})
    except Exception as e:
        logger.error(f"admin_orders error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/admin_keys', methods=['GET'])
def admin_keys():
    try:
        if not admin_required():
            return jsonify({'status': 'error', 'message': 'Invalid or missing admin_key'}), 401
        if supabase_client:
            try:
                result = supabase_client.table('api_keys').select('*').execute()
                return jsonify({'status': 'success', 'api_keys': result.data})
            except:
                pass
        return jsonify({'status': 'success', 'api_keys': list(app.fallback_api_keys.values())})
    except Exception as e:
        logger.error(f"admin_keys error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/admin_revoke', methods=['GET'])
def admin_revoke():
    try:
        if not admin_required():
            return jsonify({'status': 'error', 'message': 'Invalid or missing admin_key'}), 401
        api_key = request.args.get('api_key')
        if not api_key:
            return jsonify({'status': 'error', 'message': 'api_key parameter required'}), 400
        db_update_api_key(api_key, is_active=0)
        return jsonify({'status': 'success', 'message': f'API key {api_key} revoked'})
    except Exception as e:
        logger.error(f"admin_revoke error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/admin_verify', methods=['GET'])
def admin_verify():
    try:
        if not admin_required():
            return jsonify({'status': 'error', 'message': 'Invalid or missing admin_key'}), 401
        order_id = request.args.get('order_id')
        utr = request.args.get('utr')
        if not order_id or not utr:
            return jsonify({'status': 'error', 'message': 'order_id and utr parameters required'}), 400
        order = db_get_order(order_id)
        if not order:
            return jsonify({'status': 'error', 'message': 'Order not found'}), 404
        if order['status'] == 'verified':
            return jsonify({'status': 'error', 'message': 'Order already verified'}), 400
        if db_is_utr_verified(utr):
            return jsonify({'status': 'error', 'message': 'UTR already used'}), 400
        db_update_order(order_id, status='verified', utr=utr)
        db_mark_utr_verified(utr, order_id)
        return jsonify({'status': 'success', 'message': 'Order verified manually'})
    except Exception as e:
        logger.error(f"admin_verify error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ============================================
# PUBLIC API – NO API KEY REQUIRED
# ============================================
@app.route('/api/qr.php', methods=['GET'])
def api_qr():
    try:
        # Removed API key validation – anyone can create an order with just amount
        amount = request.args.get('amount')
        if not amount:
            return jsonify({'status': 'error', 'message': 'amount required'}), 400
        try:
            amount = float(amount)
            if amount <= 0:
                raise ValueError
        except:
            return jsonify({'status': 'error', 'message': 'Invalid amount'}), 400

        # Create order with a dummy API key (or empty) – we don't store it
        order_id = db_create_order('public', amount)
        order = db_get_order(order_id)
        if not order:
            return jsonify({'status': 'error', 'message': 'Failed to create order'}), 500

        upi_intent = f"upi://pay?pa={CONFIG['UPI_ID']}&pn=KHAN%20PAY&tr={order_id}&am={amount}&cu=INR"
        base_url = request.url_root.rstrip('/')
        qr_url = f"{base_url}/api/qr-image.php?order_id={order_id}"
        checkout_url = f"{base_url}/pay.php?order_id={order_id}"

        return jsonify({
            'status': 'success',
            'data': {
                'order_id': order_id,
                'qr_url': qr_url,
                'checkout_url': checkout_url,
                'upi_id': CONFIG['UPI_ID'],
                'amount': str(amount),
                'payable_amount': str(amount),
                'upi_intent': upi_intent,
                'created_at_ist': order['created_at'],
                'expires_at_ist': order['expires_at']
            }
        })
    except Exception as e:
        logger.error(f"api_qr error: {e}")
        return jsonify({'status': 'error', 'message': 'Failed to create order'}), 500

@app.route('/api/verify-order.php', methods=['GET'])
def api_verify_order():
    try:
        # Removed API key validation – only order_id required
        order_id = request.args.get('order_id')
        if not order_id:
            return jsonify({'status': 'error', 'message': 'order_id required'}), 400

        order = db_get_order(order_id)
        if not order:
            return jsonify({'status': 'error', 'message': 'Order not found'}), 404

        now_ist = datetime.now(IST)
        expires_ist = datetime.strptime(order['expires_at'], '%d-%m-%Y %H:%M:%S').replace(tzinfo=IST)
        if now_ist > expires_ist and order['status'] == 'pending':
            db_update_order(order_id, status='expired')
            order = db_get_order(order_id)

        if order['status'] == 'pending':
            payment = search_gmail_payment(amount=order['amount'])
            if payment:
                utr = payment.get('utr')
                if utr and not db_is_utr_verified(utr):
                    db_update_order(order_id, status='verified', utr=utr,
                                 transaction_id=payment.get('transaction_id'),
                                 sender_name=payment.get('sender'),
                                 payment_time=payment.get('date'))
                    db_mark_utr_verified(utr, order_id)
                    order = db_get_order(order_id)

        return jsonify({
            'status': 'success',
            'data': {
                'order_id': order['order_id'],
                'status': order['status'],
                'amount': order['amount'],
                'payable_amount': order['payable_amount'],
                'utr': order.get('utr'),
                'transaction_id': order.get('transaction_id'),
                'sender_name': order.get('sender_name'),
                'payment_time_ist': order.get('payment_time')
            }
        })
    except Exception as e:
        logger.error(f"api_verify_order error: {e}")
        return jsonify({'status': 'error', 'message': 'Failed to verify order'}), 500

@app.route('/api/qr-image.php', methods=['GET'])
def qr_image():
    try:
        order_id = request.args.get('order_id')
        if not order_id:
            return jsonify({'status': 'error', 'message': 'order_id required'}), 400
        order = db_get_order(order_id)
        if not order:
            return jsonify({'status': 'error', 'message': 'Order not found'}), 404

        upi_intent = f"upi://pay?pa={CONFIG['UPI_ID']}&pn=KHAN%20PAY&am={order['amount']}&cu=INR"
        if qrcode is None:
            return jsonify({'status': 'error', 'message': 'QR library not available'}), 500

        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=12,
            border=6,
        )
        qr.add_data(upi_intent)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#000000", back_color="#FFFFFF")

        img_io = BytesIO()
        img.save(img_io, 'PNG')
        img_io.seek(0)
        return send_file(img_io, mimetype='image/png')
    except Exception as e:
        logger.error(f"qr_image error: {e}")
        return jsonify({'status': 'error', 'message': 'Failed to generate QR'}), 500

# ============================================
# CACHED STATUS ENDPOINT (2s cache TTL for fast repeated checks)
# ============================================
status_cache = {}
CACHE_TTL = 2  # seconds

@app.route('/api/status', methods=['GET'])
def api_status():
    try:
        order_id = request.args.get('orderId')
        if not order_id:
            return jsonify({'error': 'orderId required'}), 400

        now = time.time()
        if order_id in status_cache:
            cached = status_cache[order_id]
            if now - cached['timestamp'] < CACHE_TTL:
                logger.info(f"Status cache hit for {order_id}")
                return jsonify(cached['response'])

        order = db_get_order(order_id)
        if not order:
            return jsonify({'error': 'Order not found'}), 404

        now_ist = datetime.now(IST)
        expires_ist = datetime.strptime(order['expires_at'], '%d-%m-%Y %H:%M:%S').replace(tzinfo=IST)
        if now_ist > expires_ist and order['status'] == 'pending':
            db_update_order(order_id, status='expired')
            order = db_get_order(order_id)

        if order['status'] == 'pending':
            payment = search_gmail_payment(amount=order['amount'])
            if payment:
                utr = payment.get('utr')
                if utr and not db_is_utr_verified(utr):
                    db_update_order(order_id, status='verified', utr=utr,
                                 transaction_id=payment.get('transaction_id'),
                                 sender_name=payment.get('sender'),
                                 payment_time=payment.get('date'))
                    db_mark_utr_verified(utr, order_id)
                    order = db_get_order(order_id)

        response = {
            'paid': order['status'] == 'verified',
            'status': order['status'],
            'amount': order['amount'],
            'utr': order.get('utr'),
            'sender': order.get('sender_name'),
            'payment_time': order.get('payment_time')
        }

        status_cache[order_id] = {
            'timestamp': now,
            'response': response
        }

        return jsonify(response)
    except Exception as e:
        logger.error(f"api_status error: {e}")
        return jsonify({'error': 'Internal error'}), 500

# ============================================
# KHAN PAY PAYMENT HTML – Auto-polling every 2 seconds
# ============================================
PAYMENT_HTML = '''
<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>KHAN PAY — Secure Payment</title>
  <meta name="description" content="Secure UPI payment verification" />
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root{--ink:#07162f;--blue:#0787f5;--cyan:#21b8ff;--green:#08c55b;--soft:#e8f6ff;--line:#cbe7fa;--white:#fff;--muted:#647792;--shadow:rgba(0,81,160,.14)}*{box-sizing:border-box}body{margin:0;color:var(--ink);font-family:Manrope,Arial,sans-serif;background-color:#f5f9ff;background-image:linear-gradient(var(--line) 1px,transparent 1px),linear-gradient(90deg,var(--line) 1px,transparent 1px);background-size:56px 56px}.page{min-height:100vh;padding:18px 14px 50px;overflow:hidden}.shell{position:relative;width:min(100%,500px);margin:auto;padding:24px;border:1.5px solid #61b9ff;border-radius:24px;background:rgba(229,245,255,.94);box-shadow:10px 12px 0 #32a9fa,0 28px 70px var(--shadow)}.brand{display:flex;align-items:center;gap:12px}.logo{display:grid;place-items:center;width:46px;height:46px;border-radius:13px;color:white;background:linear-gradient(135deg,var(--blue),var(--cyan));font-size:25px;font-weight:900;box-shadow:0 9px 22px rgba(0,133,245,.3)}.brand b{font-size:22px}.brand b em{color:var(--blue);font-style:normal}.brand small{display:block;color:var(--muted);font-size:10px;text-transform:uppercase}.secure{margin-left:auto;color:var(--green);font-weight:800;font-size:12px}.amount{margin:30px 0 20px}.live{display:inline-flex;align-items:center;gap:7px;margin-bottom:10px;padding:6px 10px;border-radius:99px;color:var(--blue);background:#d9efff;font-size:10px;font-weight:800;text-transform:uppercase}.live i{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 0 5px rgba(8,197,91,.12);animation:pulse 1.3s infinite}.amount p{margin:0 0 4px;color:var(--muted);font-size:12px;font-weight:800;text-transform:uppercase}.amount h1{margin:0;font-size:52px;line-height:1;font-weight:800}.amount h1 small{font-size:16px;color:var(--muted)}.card{padding:25px 20px 16px;text-align:center;border:1px solid #dbe8f2;border-radius:21px;background:white;box-shadow:0 15px 42px rgba(5,50,90,.08)}.qrbox{position:relative;width:min(100%,280px);aspect-ratio:1;margin:auto;padding:12px;overflow:hidden;border-radius:14px;background:white;box-shadow:0 0 0 1px #d9e6f0,0 0 42px rgba(7,135,245,.16);display:flex;align-items:center;justify-content:center}.qrbox img{display:block;width:100%;height:100%;object-fit:contain;border-radius:6px}.scan{position:absolute;z-index:3;left:10px;right:10px;top:10px;height:2px;background:var(--blue);box-shadow:0 0 10px var(--blue);animation:scan 3s ease-in-out infinite}.hint{margin:18px 0 13px}.hint b{display:block;font-size:12px;text-transform:uppercase}.hint span{font-size:10px;color:var(--muted)}button{border:0;font:inherit;cursor:pointer}.save,.done{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:43px;padding:0 22px;border-radius:11px;font-weight:700}.save{color:#075aa8;background:#edf7ff;box-shadow:0 3px 8px rgba(4,70,130,.12)}dl{margin:20px 0 0;text-align:left}dl div{display:grid;grid-template-columns:88px 1fr;gap:10px;padding:14px 0;border-top:1px solid #dce7ef;font-size:13px}dt{color:var(--muted);font-weight:600}dd{margin:0;text-align:right;font-weight:800;overflow-wrap:anywhere}.checking{display:inline-flex;align-items:center;gap:7px;padding:7px 10px;border-radius:99px;color:var(--blue);background:#dff1ff;white-space:nowrap}.checking i{width:8px;height:8px;border-radius:50%;background:var(--blue);box-shadow:0 0 0 5px rgba(7,135,245,.12);animation:pulse 1.2s infinite}.note{text-align:center;color:var(--muted);font-size:10px}.modal{position:fixed;z-index:10;inset:0;display:none;place-items:center;padding:16px;background:rgba(2,13,26,.76);backdrop-filter:blur(7px)}.modal.open{display:grid}.popup{position:relative;width:min(100%,430px);padding:38px 32px 32px;overflow:hidden;text-align:center;border:1px solid #8ee6b5;border-radius:24px;background:#f4fbff;box-shadow:0 30px 100px rgba(0,0,0,.35);animation:pop .55s cubic-bezier(.2,.9,.3,1.2)}.popup:before{content:"";position:absolute;inset:0 0 auto;height:6px;background:linear-gradient(90deg,var(--blue),var(--green),var(--cyan))}.check{display:grid;place-items:center;width:105px;height:105px;margin:0 auto 24px;border-radius:50%;color:white;background:var(--green);font-size:55px;box-shadow:0 0 0 12px #d9f8e8,0 0 46px rgba(8,197,91,.45);animation:float 3s 1s infinite}.popup label{color:var(--blue);font-size:10px;font-weight:800;text-transform:uppercase}.popup h2{margin:8px 0;color:var(--green);font-size:27px}.popup>p{margin:0 0 20px;color:var(--muted);font-size:14px}.receipt{padding:8px 16px;margin-bottom:25px;border-radius:12px;background:#edf5fa}.receipt div{display:flex;justify-content:space-between;gap:12px;padding:11px 0;border-bottom:1px solid #dbe7ef;font-size:11px;text-align:left}.receipt div:last-child{border:0}.receipt span{color:var(--muted)}.receipt b{overflow-wrap:anywhere;text-align:right}.paid{color:var(--green)}.done{width:100%;color:white;background:linear-gradient(90deg,var(--blue),var(--cyan));box-shadow:0 8px 20px rgba(7,135,245,.25)}@keyframes scan{0%,100%{transform:translateY(0);opacity:.15}50%{transform:translateY(200px);opacity:.9}}@keyframes pulse{50%{opacity:.35;transform:scale(.8)}}@keyframes pop{from{opacity:0;transform:scale(.6) rotate(-5deg)}to{opacity:1;transform:scale(1)}}@keyframes float{50%{transform:translateY(-6px)}}@media(max-width:430px){.shell{padding:18px;box-shadow:7px 8px 0 #32a9fa}.amount h1{font-size:46px}.card{padding:18px 16px}.qrbox{width:min(100%,220px)}.popup{padding:34px 22px 25px}}
  </style>
</head>
<body>
<main class="page"><section class="shell">
  <header class="brand"><div class="logo">Ҝ</div><div><b>KHAN <em>PAY</em></b><small>Secure checkout</small></div><span class="secure">✓ Secure</span></header>
  <div class="amount"><span class="live"><i></i>Live payment request</span><p>Order total</p><h1>₹<span id="amount">1.00</span> <small>INR</small></h1></div>
  <div class="card">
    <div class="qrbox"><div class="scan"></div><img id="qrImg" src="" alt="UPI QR Code"></div>
    <div class="hint"><b>Scan with any UPI app</b><span>Google Pay, PhonePe, Paytm or BHIM</span></div>
    <button class="save" id="save">⇩ Save QR</button>
    <dl><div><dt>Merchant</dt><dd id="merchant">KHAN PAY</dd></div><div><dt>Order ID</dt><dd id="order">PF-K6I078RN</dd></div><div><dt>Expires in</dt><dd id="timer">04:22</dd></div><div><dt>Verification</dt><dd><span class="checking" id="statusBadge"><i></i>Waiting for payment...</span></dd></div></dl>
  </div>
  <p class="note">✓ Protected with bank-grade security</p>
</section></main>
<!-- Success Modal -->
<div class="modal" id="modal"><section class="popup" role="dialog" aria-modal="true"><div class="check">✓</div><label>Transaction complete</label><h2>Payment successful!</h2><p>Your payment of ₹<span id="paidAmount">1.00</span> has been received.</p><div class="receipt"><div><span>Paid to</span><b>KHAN PAY</b></div><div><span>Order ID</span><b id="paidOrder"></b></div><div><span>Status</span><b class="paid">✓ Payment received</b></div></div><button class="done" id="done">Done</button></section></div>
<script>
  (function() {
    const q = new URLSearchParams(location.search);
    const amount = q.get('amount') || '1.00';
    const merchant = q.get('merchant') || 'KHAN PAY';
    const order = q.get('orderId') || 'PF-K6I078RN';
    const statusParam = q.get('status') || 'pending';

    const data = {
      amount: amount,
      merchant: merchant,
      order: order,
      status: statusParam
    };
    const $ = id => document.getElementById(id);
    $('amount').textContent = data.amount;
    $('merchant').textContent = data.merchant;
    $('order').textContent = data.order;
    $('paidAmount').textContent = data.amount;
    $('paidOrder').textContent = data.order;

    const qrImg = document.getElementById('qrImg');
    qrImg.src = '/api/qr-image.php?order_id=' + encodeURIComponent(data.order);
    qrImg.onerror = function() {
      if (typeof QRCode !== 'undefined') {
        const upi = q.get('upi') || '';
        if (upi && upi.trim() !== '') {
          const upiIntent = 'upi://pay?pa=' + encodeURIComponent(upi) +
                            '&pn=' + encodeURIComponent('KHAN PAY') +
                            '&am=' + encodeURIComponent(data.amount) +
                            '&cu=INR';
          const container = this.parentNode;
          container.innerHTML = '';
          new QRCode(container, {
            text: upiIntent,
            width: 280,
            height: 280,
            colorDark: '#000000',
            colorLight: '#ffffff',
            correctLevel: QRCode.CorrectLevel.M
          });
        }
      }
    };

    let left = 262;
    setInterval(function() {
      left = Math.max(0, left - 1);
      $('timer').textContent = String(Math.floor(left / 60)).padStart(2, '0') + ':' + String(left % 60).padStart(2, '0');
    }, 1000);

    $('save').onclick = function() {
      const img = document.getElementById('qrImg');
      if (img.src && img.src.startsWith('http')) {
        fetch(img.src)
          .then(res => res.blob())
          .then(blob => {
            const a = document.createElement('a');
            a.download = data.order + '-qr.png';
            a.href = URL.createObjectURL(blob);
            a.click();
            URL.revokeObjectURL(a.href);
          })
          .catch(() => {
            window.open(img.src, '_blank');
          });
      } else {
        const canvas = img.parentNode.querySelector('canvas');
        if (canvas) {
          const a = document.createElement('a');
          a.download = data.order + '-qr.png';
          a.href = canvas.toDataURL('image/png');
          a.click();
        }
      }
    };

    const modal = document.getElementById('modal');
    const showSuccess = function() { modal.classList.add('open'); };
    const hideSuccess = function() { modal.classList.remove('open'); };
    document.getElementById('done').onclick = hideSuccess;

    const statusBadge = document.getElementById('statusBadge');
    const statusLabel = statusBadge;
    let isSuccessShown = false;
    let checkInterval;

    function checkStatus() {
      if (isSuccessShown) return;
      const url = '/api/status?orderId=' + encodeURIComponent(data.order);
      fetch(url, { cache: 'no-cache' })
        .then(function(res) { return res.json(); })
        .then(function(resp) {
          if (resp.error) {
            console.warn('Status error:', resp.error);
            return;
          }
          if (resp.paid === true) {
            isSuccessShown = true;
            showSuccess();
            statusLabel.innerHTML = '✅ Payment received';
            statusLabel.style.background = '#d4edda';
            statusLabel.style.color = '#155724';
            clearInterval(checkInterval);
            return;
          }
          if (resp.status === 'expired') {
            statusLabel.innerHTML = '⏰ Expired';
            statusLabel.style.background = '#f8d7da';
            statusLabel.style.color = '#721c24';
            clearInterval(checkInterval);
            return;
          }
          statusLabel.innerHTML = '⏳ Waiting for payment…';
          statusLabel.style.background = '#dff1ff';
          statusLabel.style.color = '#0787f5';
        })
        .catch(function(err) { console.warn('Poll error:', err); });
    }

    if (data.status === 'verified') {
      isSuccessShown = true;
      showSuccess();
      statusLabel.innerHTML = '✅ Payment received';
      statusLabel.style.background = '#d4edda';
      statusLabel.style.color = '#155724';
    } else {
      checkStatus();
      checkInterval = setInterval(checkStatus, 2000);
    }
  })();
</script>
</body></html>
'''

# ============================================
# PAYMENT PAGE ROUTE – redirects to payment HTML with query params
# ============================================
@app.route('/pay.php', methods=['GET'])
def pay_page():
    try:
        order_id = request.args.get('order_id')
        if not order_id:
            return "Order ID missing", 400
        order = db_get_order(order_id)
        if not order:
            return "Order not found", 404

        amount = order['amount']
        merchant = CONFIG['PAYEE_NAME']
        upi = CONFIG['UPI_ID']
        orderId = order['order_id']
        status = order['status']

        payment_url = url_for('serve_payment_html',
                              amount=amount,
                              merchant=merchant,
                              orderId=orderId,
                              upi=upi,
                              status=status)
        return redirect(payment_url)
    except Exception as e:
        logger.error(f"pay_page error: {e}")
        return "Internal error", 500

# ============================================
# ROUTE TO SERVE THE PAYMENT HTML (no processing, just returns the HTML)
# ============================================
@app.route('/payment')
def serve_payment_html():
    return PAYMENT_HTML

# ============================================
# OTHER ENDPOINTS
# ============================================
@app.route('/verify-fast', methods=['GET'])
def verify_fast():
    try:
        amount = request.args.get('amount')
        utr = request.args.get('utr')
        time_window = request.args.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
        if not amount and not utr:
            return jsonify({'status': 'error', 'message': 'Provide amount or utr'}), 400
        try:
            if amount:
                amount = float(amount)
            time_window = int(time_window)
        except:
            return jsonify({'status': 'error', 'message': 'Invalid input'}), 400
        payment = search_gmail_payment(amount=amount, utr=utr, time_window=time_window)
        if payment:
            return jsonify({'status': 'success', 'message': '✅ Payment found!', 'data': payment})
        else:
            return jsonify({'status': 'not_found', 'message': f'❌ No matching payment found in last {time_window} minutes.'})
    except Exception as e:
        logger.error(f"verify_fast error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/verify-by-utr', methods=['GET', 'POST'])
def verify_by_utr():
    try:
        if request.method == 'GET':
            utr = request.args.get('utr')
            time_window = request.args.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
        else:
            data = request.get_json()
            utr = data.get('utr') if data else None
            time_window = data.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
        if not utr:
            return jsonify({'status': 'error', 'message': 'UTR required'}), 400
        try:
            time_window = int(time_window)
        except:
            return jsonify({'status': 'error', 'message': 'Invalid time_window'}), 400
        payment = search_gmail_payment(utr=utr, time_window=time_window)
        if payment:
            return jsonify({'status': 'success', 'message': '✅ Payment found by UTR', 'data': payment})
        else:
            return jsonify({'status': 'not_found', 'message': f'No payment with UTR {utr} in last {time_window} min'})
    except Exception as e:
        logger.error(f"verify_by_utr error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/verify-last-payment', methods=['GET'])
def verify_last_payment():
    try:
        amount = request.args.get('amount')
        time_window = request.args.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
        if not amount:
            return jsonify({'status': 'error', 'message': 'Amount required'}), 400
        try:
            amount = float(amount)
            time_window = int(time_window)
        except:
            return jsonify({'status': 'error', 'message': 'Invalid input'}), 400
        payment = search_gmail_payment(amount=amount, time_window=time_window)
        if payment:
            return jsonify({'status': 'success', 'message': '✅ Payment found', 'data': payment})
        else:
            return jsonify({'status': 'not_found', 'message': f'No payment of ₹{amount} in last {time_window} min'})
    except Exception as e:
        logger.error(f"verify_last_payment error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/verify-payment', methods=['GET', 'POST'])
def verify_payment_legacy():
    try:
        if request.method == 'GET':
            amount = request.args.get('amount')
            time_window = request.args.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
        else:
            data = request.get_json()
            amount = data.get('amount') if data else None
            time_window = data.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
        if not amount:
            return jsonify({'status': 'error', 'message': 'Amount required'}), 400
        try:
            amount = float(amount)
            time_window = int(time_window)
        except:
            return jsonify({'status': 'error', 'message': 'Invalid input'}), 400
        payment = search_gmail_payment(amount=amount, time_window=time_window)
        if payment:
            return jsonify({'status': 'success', 'message': '✅ Payment verified', 'data': payment})
        else:
            return jsonify({'status': 'pending', 'message': f'⏳ No payment found in last {time_window} min'})
    except Exception as e:
        logger.error(f"verify_payment error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/generate-qr', methods=['GET'])
def generate_qr_legacy():
    try:
        amount = request.args.get('amount')
        if not amount:
            return jsonify({'status': 'error', 'message': 'Amount required'}), 400
        try:
            amount = float(amount)
        except:
            return jsonify({'status': 'error', 'message': 'Invalid amount'}), 400
        upi_intent = f"upi://pay?pa={CONFIG['UPI_ID']}&pn=KHAN%20PAY&am={amount}&cu=INR"
        if qrcode is None:
            return jsonify({'status': 'error', 'message': 'QR library not available'}), 500
        qr = qrcode.QRCode(box_size=10, border=4)
        qr.add_data(upi_intent)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#000000", back_color="#FFFFFF")
        img_io = BytesIO()
        img.save(img_io, 'PNG')
        img_io.seek(0)
        return send_file(img_io, mimetype='image/png')
    except Exception as e:
        logger.error(f"generate_qr error: {e}")
        return jsonify({'status': 'error', 'message': 'Failed to generate QR'}), 500

@app.route('/debug-emails', methods=['GET'])
def debug_emails():
    try:
        payment = search_gmail_payment()
        return jsonify({'status': 'debug', 'last_payment': payment})
    except Exception as e:
        logger.error(f"debug_emails error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now(IST).isoformat(),
        'supabase_configured': bool(supabase_client),
        'gmail_configured': bool(CONFIG['GMAIL_EMAIL'] and CONFIG['GMAIL_APP_PASSWORD'])
    })

@app.route('/', methods=['GET'])
def index():
    base_url = request.url_root.rstrip('/')
    return jsonify({
        'name': 'KHAN PAY Payment Verifier',
        'version': '5.0.0',
        'description': 'Premium UI with real auto‑verification (2s polling). No API key required for QR creation or verification.',
        'endpoints': {
            'public': {
                '/': 'GET - Documentation',
                '/health': 'GET - Health check',
                '/generate-qr': 'GET - Generate colored QR (e.g., ?amount=499)',
                '/verify-fast': 'GET - Instant verify by amount or UTR',
                '/verify-by-utr': 'GET/POST - Verify by UTR only',
                '/verify-last-payment': 'GET - One-shot check by amount',
                '/verify-payment': 'GET/POST - Legacy polling',
                '/api/qr.php': 'GET - Create order and get QR (only amount required)',
                '/api/verify-order.php': 'GET - Check order status (order_id required)',
                '/api/qr-image.php': 'GET - Get colored QR image (order_id)',
                '/pay.php': 'GET - Payment page (order_id)',
                '/debug-emails': 'GET - Debug'
            }
        },
        'examples': {
            'create_order': f'curl "{base_url}/api/qr.php?amount=499"',
            'verify_order': f'curl "{base_url}/api/verify-order.php?order_id=Khan_XXXX"',
            'payment_page': f'Open in browser: {base_url}/pay.php?order_id=Khan_XXXX'
        }
    })

# ============================================
# VERCEL ENTRY POINT
# ============================================
if __name__ == '__main__':
    # This block is NOT executed on Vercel – only for local development
    app.run(host='0.0.0.0', port=5000, debug=False)