"""Server-owned orders and atomic token operations (migration 001 required)."""
import hashlib
import os
from uuid import uuid4

from fastapi import HTTPException


def promo_valid(code):
    import hmac
    expected = os.getenv('PROMO_CODE', '')
    return bool(expected and code and hmac.compare_digest(code.strip().upper(), expected.upper()))


def create_order(db, gateway, user_id, location_id, kind, product_id, product, promo=''):
    amount = product['price'] * 100
    if promo:
        if not promo_valid(promo):
            raise HTTPException(400, 'Invalid promo code')
        amount = 0
    if amount == 0:
        # A promo has one redemption per user, across all products and locations.
        digest = hashlib.sha256(f'{user_id}:{promo.strip().upper()}'.encode()).hexdigest()
        order_id = 'promo_' + digest
        existing = db.table('billing_orders').select('*').eq('order_id', order_id).execute().data
        if existing:
            previous = existing[0]
            if (previous.get('processed_at') or previous.get('product_id') != product_id
                    or previous.get('location_id') != location_id or previous.get('kind') != kind):
                raise HTTPException(409, 'This promo has already been redeemed')
            result = db.rpc('settle_order', {'p_order': order_id, 'p_payment': order_id, 'p_user': user_id}).execute().data
            return {**result, 'status': 'free_activated', 'message': 'Promo applied'}
    else:
        order = gateway.order.create(data={
            'amount': amount, 'currency': 'INR', 'receipt': uuid4().hex,
            'notes': {'user_id': user_id, 'location_id': location_id, 'kind': kind, 'product_id': product_id},
        })
        order_id = order['id']
    row = {'order_id': order_id, 'user_id': user_id, 'location_id': location_id,
           'kind': kind, 'product_id': product_id, 'amount': amount,
           'tokens': product.get('tokens', product.get('tokens_monthly', 0)),
           'duration_months': product.get('duration_months', 0)}
    db.table('billing_orders').insert(row).execute()
    if amount == 0:
        result = db.rpc('settle_order', {'p_order': order_id, 'p_payment': order_id, 'p_user': user_id}).execute().data
        return {**result, 'status': 'free_activated', 'message': 'Promo applied'}
    return {'status': 'success', 'order_id': order_id, 'amount': amount, 'currency': 'INR'}


def verify_order(db, gateway, req, kind):
    rows = db.table('billing_orders').select('*').eq('order_id', req.razorpay_order_id).eq('user_id', req.user_id).execute().data
    if not rows:
        raise HTTPException(404, 'Order not found')
    order = rows[0]
    if order['kind'] != kind or req.location_id != order.get('location_id'):
        raise HTTPException(400, 'Order does not match this purchase')
    if kind == 'subscription' and req.plan_id != order['product_id']:
        raise HTTPException(400, 'Plan does not match the original order')
    gateway.utility.verify_payment_signature({
        'razorpay_order_id': req.razorpay_order_id,
        'razorpay_payment_id': req.razorpay_payment_id,
        'razorpay_signature': req.razorpay_signature,
    })
    payment = gateway.payment.fetch(req.razorpay_payment_id)
    if (payment.get('order_id') != order['order_id'] or payment.get('amount') != order['amount']
            or payment.get('currency') != 'INR' or payment.get('status') != 'captured'):
        raise HTTPException(400, 'Payment has not been captured for the expected amount')
    return db.rpc('settle_order', {'p_order': order['order_id'], 'p_payment': payment['id'], 'p_user': req.user_id}).execute().data
