from django.db import migrations, models


def populate_original_cod_amount(apps, schema_editor):
    PaymentRequest = apps.get_model("seller", "CODPaymentApprovalRequest")
    for payment_request in PaymentRequest.objects.all().iterator():
        payment_request.original_cod_amount = payment_request.settlement_amount
        payment_request.save(update_fields=["original_cod_amount"])


class Migration(migrations.Migration):

    dependencies = [
        ("seller", "0085_codpaymentapprovalrequest"),
    ]

    operations = [
        migrations.AddField(
            model_name="codpaymentapprovalrequest",
            name="original_cod_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                max_digits=14,
            ),
        ),
        migrations.AddField(
            model_name="codpaymentapprovalrequest",
            name="return_quantities",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(
            populate_original_cod_amount,
            migrations.RunPython.noop,
        ),
    ]
