const ready = (callback) => {
  if (document.readyState !== "loading") {
    callback();
  } else {
    document.addEventListener("DOMContentLoaded", callback);
  }
};

/* Actual table switching logic */
function switchTabs(removal, enable) {
  for (const child of document.querySelectorAll(removal)) {
    child.classList.remove("current");
  }
  document.getElementById(enable).classList.add("current");
}

/* Generic tab toggling code */
function tabToggle(targets, removal) {
  for (const element of document.querySelectorAll(targets)) {
    element.addEventListener("click", (e) => {
      const tab = e.target.getAttribute("data-tab");
      switchTabs(removal, tab);
      e.target.classList.add("current");
      if (tab === "monthly") {
        document.getElementById("dedicated-checkbox").checked = false;
      }
    });
  }
}

ready(() => {
  const calculator = document.getElementById("price-calculator");
  if (calculator) {
    const catalog = JSON.parse(
      document.getElementById("calculator-packages").textContent,
    );
    const field = (name) => document.getElementById(`calculator-${name}`);
    const strings = field("strings");
    const languages = field("languages");
    const dedicated = field("dedicated");
    const numbers = new Intl.NumberFormat(document.documentElement.lang);
    const update = () => {
      const sourceCount = strings.valueAsNumber;
      const languageCount = languages.valueAsNumber;
      const total = sourceCount * (languageCount + 1);
      const empty = strings.value === "" || languages.value === "";
      const valid =
        strings.validity.valid &&
        languages.validity.valid &&
        Number.isSafeInteger(sourceCount) &&
        Number.isSafeInteger(languageCount) &&
        Number.isSafeInteger(total);
      field("prompt").hidden = !empty;
      field("error").hidden = empty || valid;
      field("result").hidden = !valid;
      if (!valid) return;

      field("total").textContent =
        `${numbers.format(sourceCount)} × (${numbers.format(languageCount)} + 1) = ${numbers.format(total)}`;
      const minimum = dedicated.checked ? catalog.dedicated_minimum : 0;
      const plan = catalog.plans.find(
        (candidate) => candidate.limit >= Math.max(total, minimum),
      );
      field("plan").hidden = !plan;
      field("custom").hidden = Boolean(plan);
      if (!plan) return;

      field("name").textContent = plan.name;
      field("capacity").textContent = numbers.format(plan.limit);
      field("monthly").textContent = plan.monthly_price;
      field("yearly").textContent = plan.yearly_price;
      field("monthly-row").hidden = dedicated.checked;
      field("trial").hidden = dedicated.checked;
      field("buy").hidden = !dedicated.checked;
      field("buy").href = plan.dedicated_url;
    };
    strings.addEventListener("input", update);
    languages.addEventListener("input", update);
    dedicated.addEventListener("change", update);
    update();
    calculator.hidden = false;
  }

  /* Mobile menu display */
  document.querySelector(".menu-show").addEventListener("click", (e) => {
    document.querySelector("body").classList.toggle("open-mobile");
    document.querySelector(".mobile-menu").classList.toggle("is-visible");
    e.preventDefault();
  });

  /* Languages menu */
  document.querySelector(".open-langs").addEventListener("click", (e) => {
    const thisParent = e.target.parentElement;
    const thisNext = e.target.nextElementSibling;
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
    ".pricing-table-tabs-menu ul li, .tab-content",
  );
  /* Dedicated hosting toggle */
  for (const element of document.querySelectorAll("#dedicated-checkbox")) {
    element.addEventListener("change", (e) => {
      const yearlyPricing = document.getElementById("yearly-pricing");
      if (e.target.checked) {
        yearlyPricing.dispatchEvent(new Event("click"));
        switchTabs(".pricing-table-tabs-menu ul li, .tab-content", "dedicated");
      } else {
        switchTabs(".pricing-table-tabs-menu ul li, .tab-content", "yearly");
      }
      yearlyPricing.classList.add("current");
    });
  }
  for (const element of document.querySelectorAll(".dedicated-toggle")) {
    element.addEventListener("click", (_e) => {
      const target = document.getElementById("dedicated-checkbox");
      target.checked = element.classList.contains("dedicated-enable");
      target.dispatchEvent(new Event("change"));
    });
  }

  /* Donate rewards selection */
  const donateInput = document.getElementById("donate-amount");
  const rewards = document.querySelector(".rewards");
  if (donateInput && rewards) {
    const selectReward = (reward) => {
      const current = rewards.querySelector(".reward.checked");
      if (current) {
        current.classList.remove("checked");
        current.setAttribute("aria-pressed", "false");
      }
      reward.classList.add("checked");
      reward.setAttribute("aria-pressed", "true");
      const rewardInput = document.getElementById(
        reward.getAttribute("data-reward-input"),
      );
      if (rewardInput) {
        rewardInput.checked = true;
      }
    };
    const chooseReward = (reward, updateAmount) => {
      const minimumAmount = Number.parseInt(
        reward.getAttribute("data-amount"),
        10,
      );
      const amount = Number.parseInt(donateInput.value, 10) || 0;
      if (updateAmount && minimumAmount > amount) {
        donateInput.value = minimumAmount;
        donateInput.dispatchEvent(new Event("change"));
      } else {
        selectReward(reward);
      }
    };
    for (const element of rewards.querySelectorAll(".reward")) {
      element.addEventListener("click", (e) => {
        chooseReward(element, true);
        e.preventDefault();
      });
    }
    donateInput.addEventListener("change", (e) => {
      const amount = Number.parseInt(e.target.value, 10) || 0;
      let availableRewards = 0;
      let highest = document.querySelector(".rewards .fourth");
      let highestAmount = Number.parseInt(
        highest.getAttribute("data-amount"),
        10,
      );
      let rewardCount = 0;
      for (const element of rewards.querySelectorAll(".reward")) {
        const currentAmount = Number.parseInt(
          element.getAttribute("data-amount"),
          10,
        );
        if (currentAmount > 0) {
          rewardCount++;
        }
        if (currentAmount <= amount) {
          element.classList.remove("small");
          if (currentAmount > 0) {
            availableRewards++;
          }
          if (highestAmount < currentAmount) {
            highest = element;
            highestAmount = currentAmount;
          }
        } else {
          element.classList.add("small");
        }
      }
      selectReward(highest);

      if (availableRewards === rewardCount) {
        document.querySelector(".whoa").classList.add("is-visible");
        document.querySelector(".nowhoa").classList.remove("is-visible");
      } else {
        document.querySelector(".whoa").classList.remove("is-visible");
        document.querySelector(".nowhoa").classList.add("is-visible");
      }
    });
    donateInput.dispatchEvent(new Event("change"));
  }

  /* VAT form */
  const vatInput = document.getElementById("id_vat_0");
  if (vatInput) {
    vatInput.addEventListener("change", (e) => {
      const value = e.target.value;
      if (value !== "") {
        document.querySelector(
          `#id_country·option[value="${value}"]`,
        ).selected = true;
      }
    });
    for (const element of document.querySelectorAll("#id_vat_0,#id_vat_1")) {
      element.addEventListener("focusout", (_e) => {
        const country = document.getElementById("id_vat_0").value;
        const code = document.getElementById("id_vat_1").value;
        if (country && code) {
          const payload = new FormData();
          payload.append("vat", country + code);
          payload.append(
            "csrfmiddlewaretoken",
            document.querySelector('input[name="csrfmiddlewaretoken"]').value,
          );
          fetch("/js/vat/", {
            method: "POST",
            body: payload,
          })
            .then((response) => response.json())
            .then((data) => {
              if (data.valid && data.name !== "---") {
                document.querySelector('input[name="name"]').value = data.name;
                const parts = data.address.trim().split("\n");
                document.querySelector('input[name="address"]').value =
                  parts[0];
                if (parts.length > 2) {
                  document.querySelector('input[name="address_2"]').value =
                    parts[1];
                }
                const cityParts = parts[parts.length - 1].split("  ");
                if (cityParts.length > 1) {
                  document.querySelector('input[name="postcode"]').value =
                    cityParts[0];
                  document.querySelector('input[name="city"]').value = cityParts
                    .slice(1)
                    .join(" ");
                } else {
                  document.querySelector('input[name="city"]').value =
                    cityParts[0];
                }
              }
            })
            .catch((error) => {
              console.error("Error:", error);
            });
        }
      });
    }
  }

  const sso = document.getElementById("SSO_Login");
  if (sso) {
    sso.submit();
  }

  for (const element of document.querySelectorAll("[data-clipboard-text]")) {
    element.addEventListener("click", (e) => {
      navigator.clipboard
        .writeText(e.target.getAttribute("data-clipboard-text"))
        .then(() => {
          e.preventDefault();
        });
    });
  }

  console.log(
    "%cStop!",
    "color: red; font-weight: bold; font-size: 50px; font-family: sans-serif; -webkit-text-stroke: 1px black;",
  );
  console.log(
    "%cThis is a browser feature intended for developers. If someone told you to copy-paste something here, they are likely trying to compromise your Weblate account.",
    "font-size: 20px; font-family: sans-serif",
  );
  console.log(
    "%cSee https://en.wikipedia.org/wiki/Self-XSS for more information.",
    "font-size: 20px; font-family: sans-serif",
  );
});
