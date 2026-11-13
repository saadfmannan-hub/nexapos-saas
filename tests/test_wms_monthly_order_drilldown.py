"""Monthly order cards and their paginated Workshop Orders results."""

import re
from datetime import date, datetime
from html import unescape
from io import BytesIO
from urllib.parse import parse_qs, urljoin, urlsplit

from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook
from pypdf import PdfReader

from apps.tenants.services import provision_business
from apps.wms_core.models import WmsRole
from apps.wms_core.services import create_wms_user
from apps.wms_orders.models import WmsWorkshopOrder
from tests.test_wms_phase1 import make_owner, make_plan
from tests.test_wms_phase2 import make_location


class MonthlyOrderDrilldownTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        plan = make_plan("Monthly order drilldown", wms=True)
        cls.owner = make_owner("monthly-orders-owner@example.test")
        business = provision_business(owner=cls.owner, name="November workshop", plan=plan)
        cls.workshop = make_location(business, "MONTH-A", "Main workshop")
        cls.archive = make_location(business, "MONTH-B", "Historical workshop")
        access = create_wms_user(
            business=business,
            full_name="Workshop Manager",
            email="monthly-orders-manager@example.test",
            password="Demo-test-password!",
            role=WmsRole.objects.get(business=business, code="workshop_manager"),
            allowed_locations=[cls.workshop],
            user=cls.owner,
        )
        cls.manager = access.membership.user
        other_owner = make_owner("monthly-orders-other@example.test")
        other = provision_business(owner=other_owner, name="Other tenant", plan=plan)
        cls.other_location = make_location(other, "MONTH-C", "Other tenant workshop")

        october = date(2026, 10, 31)
        november = date(2026, 11, 1)
        last_day = date(2026, 11, 30)
        december = date(2026, 12, 1)
        cls.finished_refs = {"OCT-RECEIVED-NOV-FINISHED", *(f"FIN-{i:02d}" for i in range(25))}
        cls.open_refs = {"NOV-IN-PROCESS", *(f"OPEN-{i:02d}" for i in range(25))}
        rows = [
            ("OCT-RECEIVED-NOV-FINISHED", cls.workshop, october, november),
            ("OCT-IN-PROCESS", cls.workshop, october, None),
            ("NOV-IN-PROCESS", cls.workshop, november, None),
            ("OCT-FINISHED", cls.workshop, october, october),
            ("DEC-FINISHED", cls.workshop, november, december),
            ("DEC-IN-PROCESS", cls.workshop, december, None),
            ("ARCHIVE-FINISHED", cls.archive, october, last_day),
            ("ARCHIVE-IN-PROCESS", cls.archive, november, None),
            ("OTHER-FINISHED", cls.other_location, october, november),
            ("OTHER-IN-PROCESS", cls.other_location, november, None),
            *((f"FIN-{i:02d}", cls.workshop, november, last_day) for i in range(25)),
            *((f"OPEN-{i:02d}", cls.workshop, last_day, None) for i in range(25)),
        ]
        WmsWorkshopOrder.objects.bulk_create(
            WmsWorkshopOrder(
                business_id=location.business_id,
                location=location,
                order_reference=reference,
                received_date=received,
                finished_date=finished,
                status=(
                    WmsWorkshopOrder.Status.FINISHED_READY if finished
                    else WmsWorkshopOrder.Status.IN_PROCESS
                ),
            )
            for reference, location, received, finished in rows
        )
        cls.archive.is_active = False
        cls.archive.save(update_fields=["is_active"])

    def report(self, **filters):
        return self.client.get(
            reverse("wms:report_monthly_orders"), {"report_month": "2026-11", **filters},
        )

    def follow_card(self, report, *, status, count_key, expected_refs, location=None):
        order_path = reverse("wms:order_list")
        href = re.search(
            rf'href="({re.escape(order_path)}\?[^"]*status={status}(?:&amp;[^"]*)?)"',
            report.content.decode(),
        )
        self.assertIsNotNone(href, f"Missing card link for {status}")
        url = unescape(href[1])
        params = parse_qs(urlsplit(url).query)
        self.assertEqual(params["month"], ["2026-11"])
        self.assertEqual(params.get("location"), [str(location.public_id)] if location else None)
        card_count = report.context["report"][count_key]
        refs = []
        while url:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "November 2026")
            self.assertContains(response, WmsWorkshopOrder.Status(status).label)
            self.assertContains(response, location.branch.name if location else "All permitted locations")
            self.assertEqual(response.context["record_count"], card_count)
            refs.extend(order.order_reference for order in response.context["page"])
            next_link = re.search(r'href="([^"]+)" aria-label="Next page"', response.content.decode())
            if next_link:
                url = urljoin(url, unescape(next_link[1]))
                next_params = parse_qs(urlsplit(url).query)
                self.assertEqual({k: v for k, v in next_params.items() if k != "page"}, params)
            else:
                url = None
        self.assertEqual(len(refs), card_count)
        self.assertCountEqual(refs, expected_refs)

    def test_card_links_match_report_at_all_and_selected_locations(self):
        self.client.force_login(self.owner)
        for location in (None, self.workshop, self.archive):
            with self.subTest(location=location):
                report = self.report(**({"location": location.public_id} if location else {}))
                self.assertEqual(report.status_code, 200)
                finished = set()
                in_process = set()
                if location != self.archive:
                    finished |= self.finished_refs
                    in_process |= self.open_refs
                if location != self.workshop:
                    finished.add("ARCHIVE-FINISHED")
                    in_process.add("ARCHIVE-IN-PROCESS")
                self.follow_card(report, status="FINISHED_READY", count_key="finished", expected_refs=finished, location=location)
                self.follow_card(report, status="IN_PROCESS", count_key="in_process", expected_refs=in_process, location=location)

    def test_restricted_user_only_sees_permitted_orders(self):
        self.client.force_login(self.manager)
        report = self.report()
        self.follow_card(report, status="FINISHED_READY", count_key="finished", expected_refs=self.finished_refs)
        self.follow_card(report, status="IN_PROCESS", count_key="in_process", expected_refs=self.open_refs)
        self.assertEqual(self.report(location=self.archive.public_id).status_code, 404)
        self.assertEqual(
            self.client.get(
                reverse("wms:order_list"),
                {"month": "2026-11", "status": "FINISHED_READY", "location": self.archive.public_id},
            ).status_code,
            404,
        )

    def test_existing_filters_and_monthly_excel_summary(self):
        self.client.force_login(self.owner)
        orders_url = reverse("wms:order_list")
        response = self.client.get(orders_url, {
            "q": "oct-", "date": "2026-10-31", "location": self.workshop.public_id,
            "status": "IN_PROCESS",
        })
        self.assertEqual([order.order_reference for order in response.context["page"]], ["OCT-IN-PROCESS"])
        response = self.client.get(
            reverse("wms:report_monthly_orders_export"), {"report_month": "2026-11"},
        )
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(BytesIO(response.content), read_only=True)
        rows = list(workbook.active.values)
        self.assertEqual(workbook.sheetnames, ["Monthly Orders"])
        self.assertIn(("Date", "Orders Received", "Orders Finished / Ready"), rows)
        self.assertEqual(rows[-1], ("Monthly totals", 53, 27))
        workbook.close()

    def download_links(self, **filters):
        response = self.client.get(reverse("wms:order_list"), {"month": "2026-11", **filters})
        self.assertEqual(response.status_code, 200)
        for format_name in ("pdf", "xlsx"):
            export_path = reverse(f"wms:order_list_{format_name}")
            href = re.search(rf'href="({re.escape(export_path)}\?[^"]+)"', response.content.decode())
            self.assertIsNotNone(href, f"Missing {format_name} download link")
            url = unescape(href[1])
            self.assertNotIn("page", parse_qs(urlsplit(url).query))
            yield format_name, url

    def test_downloads_include_all_filtered_pages_and_respect_location_scope(self):
        for user in (self.owner, self.manager):
            self.client.force_login(user)
            for status, refs, archive_ref in (
                ("FINISHED_READY", self.finished_refs, "ARCHIVE-FINISHED"),
                ("IN_PROCESS", self.open_refs, "ARCHIVE-IN-PROCESS"),
            ):
                expected_refs = refs | ({archive_ref} if user == self.owner else set())
                status_label = WmsWorkshopOrder.Status(status).label
                for format_name, url in self.download_links(status=status, page=2):
                    with self.subTest(user=user.email, status=status, format=format_name):
                        response = self.client.get(url)
                        self.assertEqual(response.status_code, 200)
                        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))
                        if format_name == "pdf":
                            pages = PdfReader(BytesIO(response.content)).pages
                            self.assertEqual(response["Content-Type"], "application/pdf")
                            self.assertIn("November workshop", pages[0].extract_text())
                            self.assertIn("November 2026", pages[0].extract_text())
                            self.assertIn("All permitted locations", pages[0].extract_text())
                            self.assertIn(f"Total matching orders: {len(expected_refs)}", pages[0].extract_text())
                            exported_refs = []
                            for page in pages:
                                page_text = page.extract_text()
                                self.assertIn("Order / Bill Reference", page_text)
                                self.assertIn(status_label, page_text)
                                exported_refs.extend(re.findall(r"^[A-Z]+(?:-[A-Z0-9]+)+$", page_text, re.MULTILINE))
                        else:
                            with BytesIO(response.content) as output:
                                workbook = load_workbook(output, read_only=True)
                                rows = list(workbook.active.values)
                                workbook.close()
                            self.assertEqual(rows[1][:2], ("Business", "November workshop"))
                            self.assertEqual(rows[2][:2], ("Month", "November 2026"))
                            self.assertEqual(rows[3][:2], ("Status", status_label))
                            self.assertEqual(rows[4][:2], ("Location", "All permitted locations"))
                            self.assertEqual(rows[5][:2], ("Total matching orders", len(expected_refs)))
                            self.assertEqual(rows[7], (
                                "Order / Bill Reference", "WMS Location", "Received Date",
                                "Eligible PCS", "Status", "Finished Date",
                            ))
                            exported_refs = [row[0] for row in rows[8:]]
                        self.assertCountEqual(exported_refs, expected_refs)

    def test_downloads_preserve_search_location_and_empty_results(self):
        self.client.force_login(self.owner)
        for query, total in (("oct-received", 1), ("no-matches", 0)):
            for format_name, url in self.download_links(
                status="FINISHED_READY", q=query, date="2026-10-31", location=self.workshop.public_id,
            ):
                with self.subTest(query=query, format=format_name):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    if format_name == "pdf":
                        pdf_text = PdfReader(BytesIO(response.content)).pages[0].extract_text()
                        self.assertIn(f"Search: {query}", pdf_text)
                        self.assertIn("Location: Main workshop", pdf_text)
                        self.assertIn(f"Total matching orders: {total}", pdf_text)
                        if total:
                            self.assertIn("OCT-RECEIVED-NOV-FINISHED", pdf_text)
                        else:
                            self.assertIn("No workshop orders match these filters.", pdf_text)
                    else:
                        workbook = load_workbook(BytesIO(response.content), read_only=True)
                        rows = list(workbook.active.values)
                        workbook.close()
                        self.assertEqual(rows[4][:2], ("Location", "Main workshop"))
                        self.assertEqual(rows[5][:2], ("Total matching orders", total))
                        self.assertEqual(rows[6][:2], ("Search", query))
                        self.assertEqual(rows[7][:2], ("Received date filter", "2026-10-31"))
                        self.assertEqual(rows[10:], [(
                            "OCT-RECEIVED-NOV-FINISHED", "Main workshop", datetime(2026, 10, 31),
                            None, "Finished / Ready", datetime(2026, 11, 1),
                        )] if total else [])

    def test_excel_keeps_order_references_as_literal_text(self):
        self.client.force_login(self.owner)
        WmsWorkshopOrder.objects.bulk_create([
            WmsWorkshopOrder(
                business_id=self.workshop.business_id,
                location=self.workshop,
                order_reference=reference,
                received_date=date(2026, 11, 1),
                eligible_piece_count=12,
            )
            for reference in ("000123", "=1+1")
        ])
        response = self.client.get(reverse("wms:order_list_xlsx"), {
            "month": "2026-11", "status": "IN_PROCESS",
        })
        workbook = load_workbook(BytesIO(response.content), read_only=True)
        cells = {row[0].value: row for row in workbook.active.iter_rows(min_row=9)}
        for reference in ("000123", "=1+1"):
            self.assertEqual(cells[reference][0].data_type, "s")
            self.assertEqual(cells[reference][0].number_format, "@")
            self.assertEqual(cells[reference][3].value, 12)
        workbook.close()

    def test_download_endpoints_enforce_permissions_and_reject_unavailable_locations(self):
        self.client.force_login(self.manager)
        params = {"month": "2026-11", "status": "FINISHED_READY"}
        for format_name in ("pdf", "xlsx"):
            url = reverse(f"wms:order_list_{format_name}")
            for location in (self.archive, self.other_location):
                self.assertEqual(self.client.get(url, {**params, "location": location.public_id}).status_code, 404)
            self.assertEqual(self.client.get(url).status_code, 404)

        role = WmsRole.objects.get(business_id=self.workshop.business_id, code="workshop_manager")
        role.permissions.remove("wms.reports.export")
        role.save(update_fields=["permissions"])
        response = self.client.get(reverse("wms:order_list"), params)
        self.assertNotContains(response, "Download PDF")
        self.assertNotContains(response, "Export Excel")
        for format_name in ("pdf", "xlsx"):
            url = reverse(f"wms:order_list_{format_name}")
            self.assertEqual(self.client.get(url, params).status_code, 403)
