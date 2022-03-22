from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional, TypeVar

import stripe
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import FieldDoesNotExist
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
)
from django.urls import path, reverse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from salesman.basket.models import BaseBasket, BaseBasketItem
from salesman.checkout.payment import PaymentError, PaymentMethod
from salesman.core.utils import get_salesman_model
from salesman.orders.models import BaseOrder, BaseOrderItem, BaseOrderPayment
from stripe.error import SignatureVerificationError, StripeError
from stripe.stripe_object import StripeObject

from .conf import app_settings

stripe.api_key = app_settings.SALESMAN_STRIPE_SECRET_KEY

logger = logging.getLogger(__name__)

BasketOrOrder = TypeVar('BasketOrOrder', BaseBasket, BaseOrder)
BasketItemOrOrderItem = TypeVar('BasketItemOrOrderItem', BaseBasketItem, BaseOrderItem)


class StripePayment(PaymentMethod):
    identifier = 'stripe'
    label = app_settings.SALESMAN_STRIPE_PAYMENT_LABEL

    def get_urls(self) -> list:
        """
        Register Stripe return views.
        """
        return [
            path('cancel/', self.cancel_view, name='stripe-cancel'),
            path('success/', self.success_view, name='stripe-success'),
            path('webhook/', self.webhook_view, name='stripe-webhook'),
        ]

    def basket_payment(self, basket: BaseBasket, request: HttpRequest) -> str:
        """
        Create Stripe session for Basket.
        """
        return self.process_payment(basket, request)

    def order_payment(self, order: BaseOrder, request: HttpRequest) -> str:
        """
        Create Stripe session for an existing Order.
        """
        return self.process_payment(order, request)

    def process_payment(self, obj: BasketOrOrder, request: HttpRequest) -> str:
        """
        Processs payment for either the Basket or Order.
        """
        try:
            session = self.get_stripe_session(obj, request)
            return session.url
        except StripeError as e:
            logger.error(e)
            raise PaymentError(str(e))

    def get_stripe_session(
        self,
        obj: BasketOrOrder,
        request: HttpRequest,
    ) -> StripeObject:
        """
        Creates a stripe checkout session object for the given Basket or Order.
        """
        session_data = self.get_stripe_session_data(obj, request)
        return stripe.checkout.Session.create(**session_data)

    def get_stripe_session_data(
        self,
        obj: BasketOrOrder,
        request: HttpRequest,
    ) -> dict:
        """
        Returns Stripe session data to be sent during checkout create.

        See available data to be set in Stripe:
        https://stripe.com/docs/api/checkout/sessions/create
        """
        customer = self.get_stripe_customer(obj.user, request)

        return {
            'mode': 'payment',
            'cancel_url': request.build_absolute_uri(reverse('stripe-cancel')),
            'success_url': request.build_absolute_uri(reverse('stripe-success')),
            'client_reference_id': self.get_reference(obj),
            'customer': customer.id,
            'line_items': [
                self.get_stripe_line_item_data(item, request)
                for item in obj.get_items()
            ],
        }

    def get_stripe_line_item_data(
        self,
        item: BasketItemOrOrderItem,
        request: HttpRequest,
    ) -> dict:
        """
        Returns Stripe session line item data.

        See available data to be set in Stripe:
        https://stripe.com/docs/api/checkout/sessions/create#create_checkout_session-line_items
        """
        return {
            'price_data': {
                'currency': self.get_currency(request),
                'unit_amount': int(item.total * 100),
                'product_data': {
                    'name': f"{item.quantity}x {item.name}",
                },
            },
            'quantity': 1,
        }

    def get_stripe_customer(
        self,
        user: AbstractUser,
        request: HttpRequest,
    ) -> StripeObject:
        """
        Creates or updates the Stripe customer.
        """
        customer_data = self.get_stripe_customer_data(user, request)
        customer_id = self.get_stripe_customer_id(user)
        if customer_id:
            try:
                customer = stripe.Customer.modify(customer_id, **customer_data)
            except StripeError:
                customer_id = None
        if not customer_id:
            customer = stripe.Customer.create(**customer_data)
            self.save_stripe_customer_id(user, customer.id)
        return customer

    def get_stripe_customer_data(
        self,
        user: AbstractUser,
        request: HttpRequest,
    ) -> dict:
        """
        Returns customer data to be save on a Stripe customer.

        See available data to be set in Stripe:
        https://stripe.com/docs/api/customers/create
        """
        return {
            'email': user.email,
            'name': user.get_full_name() or user.get_username(),
        }

    def get_stripe_customer_id(self, user: AbstractUser) -> Optional[str]:
        """
        Retrieves Stripe customer ID for the User.
        """
        return getattr(user, 'stripe_customer_id', None)

    def save_stripe_customer_id(self, user: AbstractUser, customer_id: str) -> None:
        """
        Saves the new Stripe customer ID for User.
        """
        try:
            user._meta.get_field('stripe_customer_id')
            user.stripe_customer_id = customer_id
            user.save(update_fields=['stripe_customer_id'])
        except FieldDoesNotExist:
            pass

    def refund_payment(self, payment: BaseOrderPayment) -> bool:
        """
        Refund payment on Stripe.
        """
        try:
            stripe.Refund.create(payment_intent=payment.transaction_id)
        except StripeError as e:
            logger.error(e)
            return False
        return True

    def get_currency(self, request: HttpRequest) -> str:
        """
        Returns ISO currency for the given request.
        """
        return app_settings.SALESMAN_STRIPE_DEFAULT_CURRENCY.lower()

    def get_reference(self, obj: BasketOrOrder) -> str:
        """
        Returns a Stripe reference ID for the given object used to identify the session.
        """
        if isinstance(obj, BaseBasket):
            return f'basket_{obj.id}'
        return f'order_{obj.id}'

    @classmethod
    def parse_reference(cls, reference: str) -> tuple[Optional[str], Optional[str]]:
        """
        Parses the Stripe reference ID returning the object kind and ID.
        """
        try:
            kind, id = reference.split('_')
            assert kind in ('basket', 'order')
            return kind, id
        except Exception:
            return None, None

    @classmethod
    def cancel_view(cls, request: HttpRequest) -> HttpResponse:
        """
        Handle cancelled payment on Stripe.
        """
        if app_settings.SALESMAN_STRIPE_CANCEL_URL:
            return HttpResponseRedirect(app_settings.SALESMAN_STRIPE_CANCEL_URL)
        return HttpResponse("Stripe payment cancelled")

    @classmethod
    def success_view(cls, request: HttpRequest) -> HttpResponse:
        """
        Handle successfull payment on Stripe.
        """
        if app_settings.SALESMAN_STRIPE_SUCCESS_URL:
            return HttpResponseRedirect(app_settings.SALESMAN_STRIPE_SUCCESS_URL)
        return HttpResponse("Stripe payment successfull")

    @classmethod
    @method_decorator(csrf_exempt)
    def webhook_view(cls, request: HttpRequest) -> HttpResponse:
        """
        Webhook view that is accessed asynchronously from Stripe.
        """
        payload = request.body
        sig_header = request.META.get('HTTP_STRIPE_SIGNATURE', None)
        secret = app_settings.SALESMAN_STRIPE_WEBHOOK_SECRET
        event = None

        try:
            event = stripe.Webhook.construct_event(payload, sig_header, secret)
        except ValueError as e:
            logger.error(e)
            return HttpResponseBadRequest("Invalid payload")
        except SignatureVerificationError as e:
            logger.error(e)
            return HttpResponseBadRequest("Invalid signature")

        return cls.handle_webhook_event(request, event)

    @classmethod
    def handle_webhook_event(
        cls,
        request: HttpRequest,
        event: StripeObject,
    ) -> HttpResponse:
        """
        Handles event returned from Stripe.
        """
        if event.type == 'checkout.session.completed':
            session = event.data.object
            return cls.handle_webhook_session_completed(request, session)
        return HttpResponse("Event ignored")

    @classmethod
    def handle_webhook_session_completed(
        cls,
        request: HttpRequest,
        session: StripeObject,
    ) -> HttpResponse:
        """
        Fullfill order after a successfull webhook request for session.
        """
        Basket = get_salesman_model('Basket')
        Order = get_salesman_model('Order')

        kind, id = cls.parse_reference(session.client_reference_id)
        if kind == 'basket':
            try:
                basket = Basket.objects.get(id=id)
            except BaseBasket.DoesNotExist:
                logger.error(f"Missing basket: {id}")
                return HttpResponseBadRequest("Missing basket")

            kwargs = {'status': app_settings.SALESMAN_STRIPE_PAID_STATUS}
            order = Order.objects.create_from_basket(basket, request, **kwargs)
            basket.delete()
        elif kind == 'order':
            try:
                order = Order.objects.get(id=id)
            except BaseOrder.DoesNotExist:
                logger.error(f"Missing order: {id}")
                return HttpResponseBadRequest("Missing order")
        else:
            logger.error(f"Invalid session reference: {session.id}")
            return HttpResponseBadRequest("Invalid session reference")

        # Capture payment on order.
        order.pay(
            amount=Decimal(session.amount_total / 100),
            transaction_id=session.payment_intent,
            payment_method=cls.identifier,
        )

        logger.info(f"Order fulfilled: {order.ref}")
        return HttpResponse("Order fulfilled")