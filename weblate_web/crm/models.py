from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.utils import timezone

from weblate_web.payments.models import Customer

CRM_STORAGE = FileSystemStorage(location=settings.CRM_ROOT)


class Interaction(models.Model):
    class Origin(models.IntegerChoices):
        EMAIL = 1, "Outboud e-mail"
        MERGE = 2, "Merged customer"
        ZAMMAD_ATTACHMENT = 3, "Attachment exchanged in Zammad"

    timestamp = models.DateTimeField(default=timezone.now, verbose_name="Timestamp")
    origin = models.IntegerField(choices=Origin, verbose_name="Origin")
    customer = models.ForeignKey(Customer, on_delete=models.RESTRICT)
    summary = models.CharField(max_length=200, verbose_name="Summary")
    content = models.TextField(verbose_name="Content")
    attachment = models.FileField(
        storage=CRM_STORAGE, upload_to="attachments", verbose_name="Attachment"
    )
    user = models.ForeignKey(User, null=True, on_delete=models.RESTRICT)
    remote_id = models.IntegerField(
        verbose_name="Remote ID",
        help_text="For example Zammad attachment ID",
        default=0,
    )

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.timestamp.isoformat()} [{self.customer}:{self.get_origin_display()}]: {self.summary}"


class ZammadSyncLog(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True, verbose_name="Timestamp")
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    article_id = models.IntegerField(unique=True)

    def __str__(self):
        return f"{self.customer}: {self.article_id}"
