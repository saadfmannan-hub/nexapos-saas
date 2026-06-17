"""Tests for platform announcement → business notification fan-out."""
from django.urls import reverse

from apps.accounts.models import User
from apps.notifications.models import Notification
from apps.platformadmin.models import Announcement

from .base import TenantTestCase


class AnnouncementBroadcastTests(TenantTestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="platform@nexapos.com", password="StrongPass123!",
            full_name="Platform Admin", is_superuser=True, is_staff=True,
            is_platform_admin=True)

    def _publish(self, title="Scheduled maintenance", body="Tonight 10pm UTC."):
        self.client.force_login(self.admin)
        return self.client.post(reverse("platformadmin:announcements"),
                                {"title": title, "body": body})

    def test_publish_notifies_all_active_members_of_all_businesses(self):
        r = self._publish()
        self.assertEqual(r.status_code, 302)
        # Business A has owner + cashier (2), Business B has owner (1) = 3
        notes = Notification.objects.filter(category="announcement")
        self.assertEqual(notes.count(), 3)
        recipients = set(notes.values_list("recipient__email", flat=True))
        self.assertEqual(recipients, {
            self.owner_a.email, self.cashier_a.email, self.owner_b.email})

    def test_notifications_are_tenant_scoped(self):
        self._publish()
        a_notes = Notification.objects.for_business(self.business_a).filter(
            category="announcement")
        b_notes = Notification.objects.for_business(self.business_b).filter(
            category="announcement")
        self.assertEqual(a_notes.count(), 2)   # owner_a + cashier_a
        self.assertEqual(b_notes.count(), 1)   # owner_b
        # Every row carries the recipient's own business, never the other's
        for n in a_notes:
            self.assertEqual(n.business_id, self.business_a.id)
        for n in b_notes:
            self.assertEqual(n.business_id, self.business_b.id)

    def test_announcement_content_copied(self):
        self._publish(title="New feature", body="Variant builder is live.")
        n = Notification.objects.filter(category="announcement").first()
        self.assertEqual(n.title, "New feature")
        self.assertEqual(n.body, "Variant builder is live.")
        self.assertEqual(n.severity, "info")
        self.assertFalse(n.is_read)

    def test_unread_count_increases_for_owner(self):
        before = Notification.objects.for_business(self.business_a).filter(
            recipient=self.owner_a, is_read=False).count()
        self._publish()
        after = Notification.objects.for_business(self.business_a).filter(
            recipient=self.owner_a, is_read=False).count()
        self.assertEqual(after, before + 1)

    def test_suspended_business_not_notified(self):
        self.business_b.is_active = False
        self.business_b.save(update_fields=["is_active"])
        self._publish()
        self.assertEqual(
            Notification.objects.for_business(self.business_b).filter(
                category="announcement").count(), 0)
        # Active business A still notified
        self.assertEqual(
            Notification.objects.for_business(self.business_a).filter(
                category="announcement").count(), 2)

    def test_inactive_membership_not_notified(self):
        membership = self.cashier_membership
        membership.is_active = False
        membership.save(update_fields=["is_active"])
        self._publish()
        self.assertFalse(
            Notification.objects.filter(
                category="announcement", recipient=self.cashier_a).exists())

    def test_owner_can_view_and_mark_read(self):
        self._publish()
        self.client.logout()
        self.client.force_login(self.owner_a)
        # Notifications page shows the announcement
        r = self.client.get(reverse("notifications:list"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Scheduled maintenance")
        note = Notification.objects.for_business(self.business_a).get(
            recipient=self.owner_a, category="announcement")
        # Mark as read
        r = self.client.post(reverse("notifications:mark_read", args=[note.pk]))
        self.assertEqual(r.status_code, 302)
        note.refresh_from_db()
        self.assertTrue(note.is_read)

    def test_owner_cannot_mark_other_tenants_notification(self):
        self._publish()
        other = Notification.objects.for_business(self.business_b).get(
            recipient=self.owner_b, category="announcement")
        self.client.logout()
        self.client.force_login(self.owner_a)
        # owner_a's active business is A; cannot touch B's notification
        self.client.post(reverse("notifications:mark_read", args=[other.pk]))
        other.refresh_from_db()
        self.assertFalse(other.is_read)
