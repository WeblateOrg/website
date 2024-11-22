from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.views.generic import TemplateView


class CRMBaseView(LoginRequiredMixin, TemplateView):
    title: str = "CRM"
    permission: str | None = None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = self.title
        return context

    def dispatch(self, request, *args, **kwargs):
        if self.permission is not None and not request.user.has_perm(self.permission):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class IndexView(CRMBaseView):
    template_name = "crm/index.html"
