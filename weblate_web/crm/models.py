from django.db import models

from weblate_web.payments.models import Customer


class Interaction(models.Model):
    class Origin(models.IntegerChoices):
        EMAIL = 1, "Outboud e-mail"

    timestamp = models.DateTimeField(auto_now_add=True, verbose_name="Timestamp")
    origin = models.IntegerField(choices=Origin, verbose_name="Origin")
    customer = models.ForeignKey(Customer, on_delete=models.RESTRICT)
    summary = models.CharField(max_length=200, verbose_name="Summary")
    content = models.TextField(verbose_name="Content")
    attachment = models.FileField(upload_to="crm/uploads/", verbose_name="Attachment")

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.timestamp.isoformat} [{self.customer}:{self.get_origin_display}]: {self.summary}"
