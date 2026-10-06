"""Flow approval entry point; ERPNext owns PayPal validation and accounting."""

from flow.lib.tool import tool


@tool(requires_confirmation=True)
def paypal_receipt_procedure(
    sales_order: str, gross: float, fee: float, net: float, posting_date: str,
    transaction_id: str, reference_date: str, customer: str = "",
    currency: str = "USD", dry_run: bool = False,
):
    """登记已人工确认到账的 PayPal 美元订单预收款；只读预检不写入。

    返回结果中的 actual_receipt_currency/actual_receipt_note 是实际 PayPal 收款币种；
    company_base_currency/base_currency_note 只是 ERP 总账折算币种。展示时必须分开，
    不得把公司本位币写成实际收到的币种，也不得暗示发生换汇或提现。
    """
    from erpnext.accounts.doctype.payment_entry.paypal_receipt import (
        paypal_receipt_procedure as register_receipt,
    )

    return register_receipt(
        sales_order=sales_order, gross=gross, fee=fee, net=net,
        posting_date=posting_date, transaction_id=transaction_id,
        reference_date=reference_date, customer=customer, currency=currency, dry_run=dry_run,
    )
