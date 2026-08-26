"""CSV / Excel / PDF export builders for report datasets."""
import csv
from datetime import date, datetime
from decimal import Decimal

from django.http import HttpResponse

from .pdf import render_pdf

FORMULA_PREFIXES = ("=", "+", "-", "@")


def _formula_safe(value):
    """Prevent user-controlled text from becoming a spreadsheet formula."""
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    if stripped and stripped.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def _cell(value):
    if value is None or value == "":
        return "-"
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (date, datetime)):
        return str(value)
    return _formula_safe(value)


def _csv_cell(value):
    if value is None or value == "":
        return "-"
    return _formula_safe(value)


def export_csv(title, data):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{title}.csv"'
    writer = csv.writer(response)
    writer.writerow([_csv_cell(v) for v in data["columns"]])
    for row in data["rows"]:
        writer.writerow([_csv_cell(v) for v in row])
    if data.get("totals"):
        writer.writerow([_csv_cell(v) for v in data["totals"]])
    if data.get("summary"):
        writer.writerow([])
        for label, value in data["summary"]:
            writer.writerow([label, _csv_cell(value)])
    return response


def export_xlsx(title, data):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="0F172A")
    ws.append([_cell(v) for v in data["columns"]])
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
    for row in data["rows"]:
        ws.append([_cell(v) for v in row])
    if data.get("totals"):
        ws.append([_cell(v) for v in data["totals"]])
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
    data_end_row = ws.max_row
    if data.get("summary"):
        ws.append([])
        for label, value in data["summary"]:
            ws.append([_cell(label), _cell(value)])
            ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
    for index, number_format in data.get("column_formats", {}).items():
        column = int(index) + 1
        for row in range(2, data_end_row + 1):
            ws.cell(row=row, column=column).number_format = number_format
    for idx, col in enumerate(data["columns"], start=1):
        width = max(len(str(col)) + 2, 12)
        if idx == 1 and data.get("summary"):
            width = max(width, max(len(str(label)) + 2 for label, _ in data["summary"]))
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = width
    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument"
                     ".spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="{title}.xlsx"'
    wb.save(response)
    return response


def export_expense_analysis_xlsx(title, data):
    """Export the monthly expense report as three focused worksheets."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="0F172A")
    money_format = "#,##0.000"

    def style_header(sheet, row):
        for cell in sheet[row]:
            cell.font = header_font
            cell.fill = header_fill

    summary = wb.active
    summary.title = "Monthly Summary"
    summary.append(["Monthly Expense Report", data.get("month_label", "")])
    summary["A1"].font = Font(bold=True, size=14)
    summary.append(["Filters", "; ".join(data.get("applied_filters", [])) or "All"])
    summary.append([])
    summary.append(["Measure", "Value"])
    style_header(summary, 4)
    summary_rows = (
        ("Total Expenses", data["total_expenses"]),
        ("Transaction Count", data["transaction_count"]),
        ("Average Expense", data["average_expense"]),
        (
            "Highest Expense Day",
            (
                data["highest_expense_day"]["date"]
                if data.get("highest_expense_day") else "-"
            ),
        ),
        (
            "Highest Expense Day Total",
            (
                data["highest_expense_day"]["total"]
                if data.get("highest_expense_day") else Decimal("0")
            ),
        ),
    )
    for label, value in summary_rows:
        summary.append([label, _cell(value)])
    summary["B5"].number_format = money_format
    summary["B7"].number_format = money_format
    summary["B9"].number_format = money_format
    summary.append([])
    summary.append(["Paid Via", "Count", "Total"])
    style_header(summary, summary.max_row)
    for item in data["payment_breakdown"]:
        summary.append([item["label"], item["count"], item["total"]])
        summary.cell(summary.max_row, 3).number_format = money_format
    summary.append([])
    summary.append(data["columns"])
    style_header(summary, summary.max_row)
    for row in data["rows"]:
        summary.append([_cell(value) for value in row])
        summary.cell(summary.max_row, 3).number_format = money_format
        summary.cell(summary.max_row, 4).number_format = money_format
    summary.column_dimensions["A"].width = 28
    summary.column_dimensions["B"].width = 24
    summary.column_dimensions["C"].width = 18
    summary.column_dimensions["D"].width = 18
    summary.column_dimensions["E"].width = 14

    daily = wb.create_sheet("Daily Breakdown")
    daily_columns = [
        "Date", "Transactions", "Cash", "Card", "Bank / Online",
        "Other", "Daily Total",
    ]
    daily.append(daily_columns)
    style_header(daily, 1)
    for item in data["daily_breakdown"]:
        daily.append([
            item["date"], item["count"], item["cash"], item["card"],
            item["bank_online"], item["other"], item["total"],
        ])
        for column in range(3, 8):
            daily.cell(daily.max_row, column).number_format = money_format
    daily.freeze_panes = "A2"
    daily.column_dimensions["A"].width = 14
    for letter in ("B", "C", "D", "E", "F", "G"):
        daily.column_dimensions[letter].width = 16

    transactions = wb.create_sheet("Expense Transactions")
    transaction_columns = [
        "Date", "Expense Number", "Expense / Payee", "Category", "Branch",
        "Paid Via", "Amount", "Status", "Source",
    ]
    transactions.append(transaction_columns)
    style_header(transactions, 1)
    for item in data["transactions"]:
        transactions.append([
            item["date"], _formula_safe(item["number"]),
            _formula_safe(item["payee"]), _formula_safe(item["category"]),
            _formula_safe(item["branch"]), item["paid_via"], item["amount"],
            item["status"], item["source"],
        ])
        transactions.cell(transactions.max_row, 7).number_format = money_format
    transactions.freeze_panes = "A2"
    widths = (14, 20, 28, 22, 20, 22, 16, 16, 14)
    for index, width in enumerate(widths, start=1):
        transactions.column_dimensions[
            transactions.cell(1, index).column_letter
        ].width = width

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument"
                     ".spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="{title}.xlsx"'
    wb.save(response)
    return response


def export_pdf(title, data, business, filters_label=""):
    pdf = render_pdf("reports/report_pdf.html", {
        "title": title, "data": data, "business": business,
        "filters_label": filters_label,
    })
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{title}.pdf"'
    return response


def export_expense_analysis_pdf(title, data, business):
    pdf = render_pdf("reports/expense_analysis_pdf.html", {
        "title": title,
        "data": data,
        "business": business,
    })
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{title}.pdf"'
    return response
