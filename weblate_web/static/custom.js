var _paq = window._paq || [];
/* tracker methods like "setCustomDimension" should be called before "trackPageView" */
_paq.push(["disableCookies"]);
_paq.push(["trackPageView"]);
_paq.push(["enableLinkTracking"]);
_paq.push(["setTrackerUrl", "https://stats.cihar.com/matomo.php"]);
_paq.push(["setSiteId", "12"]);

var ready = (callback) => {
  if (document.readyState != "loading") {
    callback();
  } else {
    document.addEventListener("DOMContentLoaded", callback);
  }
};

/* Generic tab toggling code */
function tabToggle(targets, removal) {
  document.querySelectorAll(targets).forEach((element) => {
    element.addEventListener("click", (e) => {
      document.querySelectorAll(removal).forEach((child) => {
        child.classList.remove("current");
      });
      e.target.classList.add("current");
      document
        .getElementById(e.target.getAttribute("data-tab"))
        .classList.add("current");
    });
  });
}

ready(() => {
  /* Mobile menu display */
  document.querySelector(".menu-show").addEventListener("click", (e) => {
    document.querySelector("body").classList.toggle("open-mobile");
    document.querySelector(".mobile-menu").classList.toggle("is-visible");
    e.preventDefault();
  });

  /* Languages menu */
  document.querySelector(".open-langs").addEventListener("click", (e) => {
    console.log(e);
    var thisParent = e.target.parentElement;
    var thisNext = e.target.nextElementSibling;
    console.log(thisNext);
    if (thisParent.classList.contains("opened")) {
      thisParent.classList.remove("opened");
      thisNext.style.opacity = "0";
      setTimeout(() => {
        thisNext.classList.toggle("is-visible");
        thisNext.style.opacity = null;
      }, 350);
    } else {
      thisNext.style.display = "block";
      thisParent.classList.add("opened");
      setTimeout(() => {
        thisNext.classList.toggle("is-visible");
        thisNext.style.display = null;
      }, 20);
    }
    e.preventDefault();
  });

  /* Pricing tabs */
  tabToggle("ul.pricing-tabs li", "ul.pricing-tabs li, .tab-content");

  /* Yearly/monthly toggle */
  tabToggle(
    ".pricing-table-tabs-menu ul li",
    ".pricing-table-tabs-menu ul li, .tab-content"
  );

  /* Donate rewards selection */
  let donate_input = document.getElementById("donate-amount");
  if (donate_input) {
    document.querySelectorAll(".rewards .choose").forEach((element) => {
      element.addEventListener("click", (e) => {
        var container = e.target.parentElement;
        container.parentElement
          .querySelector(".reward.checked")
          .classList.remove("checked");
        container.classList.add("checked");
        container.querySelector("input").checked = true;
        e.preventDefault();
      });
    });
    document.querySelectorAll(".rewards .close").forEach((element) => {
      element.addEventListener("click", (e) => {
        document
          .querySelector(".rewards .fourth .choose")
          .dispatchEvent(new Event("click"));
        e.preventDefault();
      });
    });
    donate_input.addEventListener("change", (e) => {
      var amount = parseInt(e.target.value);
      var found = 0;
      var highest = document.querySelector(".rewards .fourth");
      var highest_amount = parseInt(highest.getAttribute("data-amount"));
      document.querySelectorAll(".reward").forEach((element) => {
        var current_amount = parseInt(element.getAttribute("data-amount"));
        if (current_amount <= amount) {
          element.classList.remove("small");
          found++;
          if (highest_amount < current_amount) {
            highest = element;
            highest_amount = current_amount;
          }
        } else {
          element.classList.add("small");
          if (element.classList.contains("checked")) {
            element.querySelector(".close").dispatchEvent(new Event("click"));
          }
        }
      });
      highest.querySelector(".choose").click();

      if (found > 1) {
        document.querySelector(".whoa").classList.add("is-visible");
        document.querySelector(".nowhoa").classList.remove("is-visible");
      } else {
        document.querySelector(".whoa").classList.remove("is-visible");
        document.querySelector(".nowhoa").classList.add("is-visible");
      }

      console.log(amount);
    });
    donate_input.dispatchEvent(new Event("change"));
  }

  new ClipboardJS("[data-clipboard-text]");
});

$(function () {
  $("#id_vat_0").on("change", function () {
    var value = $(this).val();
    if (value != "") {
      var country = $('#id_country option[value="' + value + '"]');
      country.prop("selected", true);
    }
  });
  $("#id_vat_0,#id_vat_1").on("focusout", function () {
    var country = $("#id_vat_0").val();
    var code = $("#id_vat_1").val();
    if (country && code) {
      var payload = {
        vat: country + code,
        payment: $('input[name="payment"]').val(),
        csrfmiddlewaretoken: $('input[name="csrfmiddlewaretoken"]').val(),
      };
      $.post("/js/vat/", payload, function (data) {
        if (data.valid) {
          $('input[name="name"]').val(data.name);
          var parts = data.address.trim().split("\n");
          $('input[name="address"]').val(parts[0]);
          $('input[name="city"]').val(parts[parts.length - 1]);
        }
      });
    }
  });
});
