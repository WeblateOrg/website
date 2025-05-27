from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.storage import FileSystemStorage
from django.db import models

from weblate_web.payments.models import Customer

CRM_STORAGE = FileSystemStorage(location=settings.CRM_ROOT)


class Interaction(models.Model):
    class Origin(models.IntegerChoices):
        EMAIL = 1, "Outboud e-mail"
        MERGE = 2, "Merged customer"

    timestamp = models.DateTimeField(auto_now_add=True, verbose_name="Timestamp")
    origin = models.IntegerField(choices=Origin, verbose_name="Origin")
    customer = models.ForeignKey(Customer, on_delete=models.RESTRICT)
    summary = models.CharField(max_length=200, verbose_name="Summary")
    content = models.TextField(verbose_name="Content")
    attachment = models.FileField(
        storage=CRM_STORAGE, upload_to="attachments", verbose_name="Attachment"
    )
    user = models.ForeignKey(User, null=True, on_delete=models.RESTRICT)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.timestamp.isoformat} [{self.customer}:{self.get_origin_display}]: {self.summary}"
