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

const copyText = async (text) => {
  if (navigator.clipboard?.writeText && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.left = "-9999px";
  textarea.style.position = "fixed";
  textarea.style.top = "0";
  document.body.append(textarea);
  textarea.focus();
  textarea.select();

  try {
    if (!document.execCommand("copy")) {
      throw new Error("Copy command failed");
    }
  } finally {
    textarea.remove();
  }
};

const initClipboardActions = () => {
  const buttons = document.querySelectorAll("[data-crm-copy-text]");
  if (!buttons.length) {
    return;
  }

  buttons.forEach((button) => {
    const defaultLabel = button.getAttribute("aria-label") || "";
    const defaultTitle = button.getAttribute("title") || "";
    const status = button.parentElement?.querySelector(
      "[data-crm-copy-status]",
    );
    let resetTimeout = null;

    const resetButton = () => {
      button.classList.remove("is-copied", "is-error");
      button.setAttribute("aria-label", defaultLabel);
      if (defaultTitle) {
        button.setAttribute("title", defaultTitle);
      }
      if (status) {
        status.textContent = "";
      }
    };

    const showStatus = (message, className) => {
      button.classList.remove("is-copied", "is-error");
      button.classList.add(className);
      button.setAttribute("aria-label", message);
      if (defaultTitle) {
        button.setAttribute("title", message);
      }
      if (status) {
        status.textContent = message;
      }

      if (resetTimeout) {
        window.clearTimeout(resetTimeout);
      }
      resetTimeout = window.setTimeout(resetButton, 2000);
    };

    button.addEventListener("click", async () => {
      const text = button.dataset.crmCopyText;
      if (!text) {
        return;
      }

      try {
        await copyText(text);
      } catch {
        showStatus(button.dataset.crmCopyError || defaultLabel, "is-error");
        return;
      }

      showStatus(button.dataset.crmCopySuccess || defaultLabel, "is-copied");
    });
  });
};

if (document.readyState !== "loading") {
  updateCrmMenuTabStop();
  initInvoiceConfirmation();
  initClipboardActions();
} else {
  document.addEventListener("DOMContentLoaded", () => {
    updateCrmMenuTabStop();
    initInvoiceConfirmation();
    initClipboardActions();
  });
}
