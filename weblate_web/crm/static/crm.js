const updateCrmMenuTabStop = () => {
  const toggle = document.getElementById("crm-menu-toggle");
  if (!toggle || !window.matchMedia) {
    return;
  }

  const mobileNavigation = window.matchMedia("(max-width: 980px)");
  const syncMenuTabStop = () => {
    toggle.tabIndex = mobileNavigation.matches ? 0 : -1;
  };

  syncMenuTabStop();
  if (mobileNavigation.addEventListener) {
    mobileNavigation.addEventListener("change", syncMenuTabStop);
  } else {
    mobileNavigation.addListener(syncMenuTabStop);
  }
};

const initInvoiceConfirmation = () => {
  const forms = document.querySelectorAll("[data-crm-invoice-confirm]");
  if (!forms.length) {
    return;
  }

  const dialog = document.getElementById("crm-invoice-confirm-dialog");
  const dialogMessage = dialog?.querySelector(
    "[data-crm-invoice-confirm-message]",
  );
  let pendingForm = null;
  let pendingSubmitter = null;

  const isInvoiceSubmission = (form) => {
    if (form.hasAttribute("data-crm-invoice-always")) {
      return true;
    }

    const invoiceKind = form.dataset.crmInvoiceKind;
    const selectedKind = form.querySelector('input[name="kind"]:checked');
    return Boolean(invoiceKind && selectedKind?.value === invoiceKind);
  };

  const getConfirmationField = (form) => {
    let field = form.querySelector('input[name="confirm_invoice"]');
    if (!field) {
      field = document.createElement("input");
      field.type = "hidden";
      field.name = "confirm_invoice";
      form.append(field);
    }
    return field;
  };

  const submitConfirmed = (form, submitter) => {
    getConfirmationField(form).value = "1";
    if (form.requestSubmit && submitter) {
      form.requestSubmit(submitter);
    } else if (form.requestSubmit) {
      form.requestSubmit();
    } else {
      form.submit();
    }
  };

  const openDialog = (form, submitter) => {
    const message = form.dataset.crmInvoiceConfirmMessage;
    if (!dialog?.showModal) {
      if (window.confirm(message)) {
        submitConfirmed(form, submitter);
      }
      return;
    }

    if (dialogMessage && message) {
      dialogMessage.textContent = message;
    }
    pendingForm = form;
    pendingSubmitter = submitter;
    dialog.returnValue = "";
    dialog.showModal();
  };

  dialog?.addEventListener("close", () => {
    const confirmed = dialog.returnValue === "confirm";
    dialog.returnValue = "";
    if (confirmed && pendingForm) {
      submitConfirmed(pendingForm, pendingSubmitter);
    }
    pendingForm = null;
    pendingSubmitter = null;
  });

  forms.forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!isInvoiceSubmission(form)) {
        return;
      }

      if (getConfirmationField(form).value === "1") {
        return;
      }

      event.preventDefault();
      openDialog(form, event.submitter);
    });
  });
};

if (document.readyState !== "loading") {
  updateCrmMenuTabStop();
  initInvoiceConfirmation();
} else {
  document.addEventListener("DOMContentLoaded", () => {
    updateCrmMenuTabStop();
    initInvoiceConfirmation();
  });
}
