from django.template import Library
from django.utils import timezone

register = Library()


@register.filter
def days_diff_from_today(end):
    return (end - timezone.now()).days + 1
