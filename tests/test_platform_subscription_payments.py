"""Focused accounting and security tests for Platform Admin payments."""
from datetime import timedelta
from decimal import Decimal

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.subscriptions.models import Subscription, SubscriptionPayment

from .base import TenantTestCase


class PlatformSubscriptionPaymentControlTests(TenantTestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="payment-admin@nexapos.com",
            password="StrongPass123!",
            full_name="Payment Admin",
            is_staff=True,
            is_platform_admin=True,
        )
        self.client.force_login(self.admin)
        self.subscription = self.business_a.subscription

    def create_payment(self, *, business=None, amount="100.000", **kwargs):
        business = business or self.business_a
        subscription = business.subscription
        defaults = {
            "business": business,
            "subscription": subscription,
            "amount": Decimal(amount),
            "currency_code": subscription.plan.currency_code,
            "method": "bank_transfer",
            "reference": "PAY-EDIT-1",
            "payment_date": timezone.localdate(),
            "recorded_by": self.admin,
            "notes": "Original note",
        }
        defaults.update(kwargs)
        return SubscriptionPayment.objects.create(**defaults)

    def edit_url(self, payment, business=None):
        return reverse(
            "platformadmin:payment_edit",
            args=[(business or self.business_a).public_id, payment.public_id],
        )

    def reverse_url(self, payment, business=None):
        return reverse(
            "platformadmin:payment_reverse",
            args=[(business or self.business_a).public_id, payment.public_id],
        )

    def edit_payload(self, **overrides):
        payload = {
            "payment_date": str(timezone.localdate()),
            "method": "manual",
            "reference": "RECEIPT-UPDATED",
            "amount": "100.000",
            "notes": "Corrected by platform admin",
        }
        payload.update(overrides)
        return payload

    def create_renewal_payment(self):
        self.subscription.status = Subscription.Status.ACTIVE
        self.subscription.current_period_start = timezone.now() - timedelta(days=20)
        self.subscription.current_period_end = timezone.now() + timedelta(days=10)
        self.subscription.save()
        original_state = {
            "start": self.subscription.current_period_start,
            "end": self.subscription.current_period_end,
            "status": self.subscription.status,
            "plan_id": self.subscription.plan_id,
        }
        response = self.client.post(
            reverse(
                "platformadmin:business_action",
                args=[self.business_a.public_id, "renew"],
            ),
            {
                "renew-plan": self.subscription.plan_id,
                "renew-renewal_type": "monthly",
                "renew-start_date": "",
                "renew-end_date": "",
                "renew-payment_amount": "150.000",
                "renew-payment_method": "bank_transfer",
                "renew-payment_reference": "RENEWAL-LINKED",
                "renew-notes": "Linked renewal",
            },
        )
        self.assertEqual(response.status_code, 302)
        return (
            SubscriptionPayment.objects.get(reference="RENEWAL-LINKED"),
            original_state,
        )

    def test_platform_admin_can_edit_method_and_reference_with_audit(self):
        payment = self.create_payment(method="bank_transfer")

        response = self.client.post(self.edit_url(payment), self.edit_payload())

        self.assertRedirects(
            response,
            reverse(
                "platformadmin:business_detail",
                args=[self.business_a.public_id],
            ),
        )
        payment.refresh_from_db()
        self.assertEqual(payment.method, "manual")
        self.assertEqual(payment.reference, "RECEIPT-UPDATED")
        self.assertEqual(payment.recorded_by, self.admin)
        log = AuditLog.objects.get(action="platform.subscription_payment_edited")
        self.assertEqual(log.old_values["method"], "bank_transfer")
        self.assertEqual(log.new_values["method"], "manual")
        self.assertEqual(log.new_values["reference"], "RECEIPT-UPDATED")

    def test_edit_amount_updates_active_total_without_changing_expiry(self):
        payment, _ = self.create_renewal_payment()
        self.subscription.refresh_from_db()
        renewed_end = self.subscription.current_period_end

        response = self.client.post(
            self.edit_url(payment),
            self.edit_payload(amount="120.000"),
        )

        self.assertEqual(response.status_code, 302)
        payment.refresh_from_db()
        self.subscription.refresh_from_db()
        self.assertEqual(payment.amount, Decimal("120.000"))
        self.assertEqual(self.subscription.current_period_end, renewed_end)
        detail = self.client.get(reverse(
            "platformadmin:business_detail",
            args=[self.business_a.public_id],
        ))
        self.assertEqual(detail.context["payment_summary"]["total"], Decimal("120.000"))

    def test_edit_rejects_non_positive_amount(self):
        payment = self.create_payment()

        response = self.client.post(
            self.edit_url(payment),
            self.edit_payload(amount="0.000"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context["form"], "amount", "Amount must be greater than zero.")
        payment.refresh_from_db()
        self.assertEqual(payment.amount, Decimal("100.000"))

    def test_duplicate_payment_can_be_reversed_and_valid_payment_remains(self):
        valid = self.create_payment(reference="VALID", amount="80.000")
        duplicate = self.create_payment(reference="DUPLICATE", amount="80.000")

        response = self.client.post(
            self.reverse_url(duplicate),
            {"reversal_reason": "Duplicate bank receipt"},
        )

        self.assertEqual(response.status_code, 302)
        duplicate.refresh_from_db()
        self.assertTrue(duplicate.is_reversed)
        self.assertEqual(duplicate.reversed_by, self.admin)
        self.assertEqual(duplicate.reversal_reason, "Duplicate bank receipt")
        self.assertTrue(SubscriptionPayment.objects.filter(pk=duplicate.pk).exists())
        self.assertEqual(
            SubscriptionPayment.objects.active().get(),
            valid,
        )
        detail = self.client.get(reverse(
            "platformadmin:business_detail",
            args=[self.business_a.public_id],
        ))
        self.assertEqual(detail.context["payment_summary"]["total"], Decimal("80.000"))
        self.assertContains(detail, "Reversed")

    def test_reversing_renewal_linked_payment_restores_exact_expiry(self):
        payment, original = self.create_renewal_payment()
        self.subscription.refresh_from_db()
        self.assertNotEqual(self.subscription.current_period_end, original["end"])
        self.assertTrue(payment.subscription_state_before)
        self.assertTrue(payment.subscription_state_after)

        response = self.client.post(
            self.reverse_url(payment),
            {"reversal_reason": "Renewal was entered twice"},
        )

        self.assertEqual(response.status_code, 302)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.current_period_start, original["start"])
        self.assertEqual(self.subscription.current_period_end, original["end"])
        self.assertEqual(self.subscription.status, original["status"])
        self.assertEqual(self.subscription.plan_id, original["plan_id"])

    def test_reversal_audit_contains_reason_and_subscription_states(self):
        payment, original = self.create_renewal_payment()

        self.client.post(
            self.reverse_url(payment),
            {"reversal_reason": "Wrong customer renewal"},
        )

        log = AuditLog.objects.get(action="platform.subscription_payment_reversed")
        self.assertEqual(log.business, self.business_a)
        self.assertEqual(log.object_id, str(payment.public_id))
        self.assertIsNone(log.old_values["payment"]["reversed_at"])
        self.assertEqual(
            log.new_values["payment"]["reversal_reason"],
            "Wrong customer renewal",
        )
        self.assertEqual(
            log.new_values["subscription"]["current_period_end"],
            original["end"].isoformat(),
        )

    def test_non_platform_admin_cannot_edit_or_reverse(self):
        payment = self.create_payment()
        self.client.force_login(self.owner_a)

        edit_response = self.client.get(self.edit_url(payment))
        reverse_response = self.client.post(
            self.reverse_url(payment),
            {"reversal_reason": "Unauthorized"},
        )

        self.assertEqual(edit_response.status_code, 403)
        self.assertEqual(reverse_response.status_code, 403)
        payment.refresh_from_db()
        self.assertFalse(payment.is_reversed)

    def test_cross_business_payment_access_is_hidden(self):
        payment = self.create_payment(business=self.business_b)

        edit_response = self.client.get(self.edit_url(payment, self.business_a))
        reverse_response = self.client.post(
            self.reverse_url(payment, self.business_a),
            {"reversal_reason": "Cross-business attempt"},
        )

        self.assertEqual(edit_response.status_code, 404)
        self.assertEqual(reverse_response.status_code, 404)
        payment.refresh_from_db()
        self.assertFalse(payment.is_reversed)

    def test_already_reversed_payment_cannot_be_reversed_or_edited(self):
        payment = self.create_payment()
        self.client.post(
            self.reverse_url(payment),
            {"reversal_reason": "First reversal"},
        )

        second = self.client.post(
            self.reverse_url(payment),
            {"reversal_reason": "Second reversal"},
        )
        edit = self.client.get(self.edit_url(payment))

        self.assertEqual(second.status_code, 400)
        self.assertEqual(edit.status_code, 400)
        self.assertEqual(
            AuditLog.objects.filter(
                action="platform.subscription_payment_reversed",
            ).count(),
            1,
        )

    def test_reverse_requires_post_reason_and_csrf(self):
        payment = self.create_payment()

        self.assertEqual(self.client.get(self.reverse_url(payment)).status_code, 405)
        self.assertEqual(self.client.post(self.reverse_url(payment), {}).status_code, 400)

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        csrf_response = csrf_client.post(
            self.reverse_url(payment),
            {"reversal_reason": "No token"},
        )
        self.assertEqual(csrf_response.status_code, 403)
        payment.refresh_from_db()
        self.assertFalse(payment.is_reversed)

    def test_business_detail_shows_actions_and_reversed_rows(self):
        active = self.create_payment(reference="ACTIVE-ROW")
        reversed_payment = self.create_payment(reference="REVERSED-ROW")
        self.client.post(
            self.reverse_url(reversed_payment),
            {"reversal_reason": "Display reversal"},
        )

        response = self.client.get(reverse(
            "platformadmin:business_detail",
            args=[self.business_a.public_id],
        ))

        self.assertContains(response, self.edit_url(active))
        self.assertContains(response, self.reverse_url(active))
        self.assertContains(response, "Reverse payment")
        self.assertContains(response, "Reversed")
        self.assertNotContains(response, self.edit_url(reversed_payment))
