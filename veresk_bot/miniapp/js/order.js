/* global goTo, tg */

const orderData = {
  name: "",
  phone: "",
  date: "",
  recipient: "",
  occasion: "",
  relation: "",
  budget: "",
};

const TOTAL_STEPS = 6;
const PROFILE_STEPS = 2;
let currentStep = 1;
let skipProfileSteps = false;

function getInitData() {
  return tg?.initData || "";
}

function apiHeaders() {
  return { "X-Telegram-Init-Data": getInitData() };
}

async function fetchClientProfile() {
  try {
    const resp = await fetch("/api/client/me", { headers: apiHeaders() });
    if (!resp.ok) return null;
    const data = await resp.json();
    return data.known ? data : null;
  } catch (e) {
    console.error("Client profile fetch failed:", e);
    return null;
  }
}

function visibleStep() {
  return skipProfileSteps ? currentStep - PROFILE_STEPS : currentStep;
}

function visibleTotal() {
  return skipProfileSteps ? TOTAL_STEPS - PROFILE_STEPS : TOTAL_STEPS;
}

function minStep() {
  return skipProfileSteps ? PROFILE_STEPS + 1 : 1;
}

function updateProgress() {
  const step = visibleStep();
  const total = visibleTotal();
  const pct = Math.round((step / total) * 100);
  const fill = document.getElementById("progress-fill");
  const text = document.getElementById("progress-text");
  if (fill) fill.style.width = `${pct}%`;
  if (text) text.textContent = `Шаг ${step} из ${total}`;
}

function showStep(step) {
  currentStep = step;
  document.querySelectorAll(".order-step").forEach((el) => {
    el.classList.toggle("active", Number(el.dataset.step) === step);
  });
  document.getElementById("btn-step-prev")?.classList.toggle("hidden", step <= minStep());
  const isLast = step >= TOTAL_STEPS;
  document.getElementById("btn-step-next")?.classList.toggle("hidden", isLast);
  document.getElementById("btn-submit")?.classList.toggle("hidden", !isLast);
  updateProgress();
  if (tg) tg.BackButton.show(step > minStep() || getCurrentScreen?.() !== "home");
}

function getCurrentScreen() {
  const active = document.querySelector(".screen.active");
  return active?.id?.replace("screen-", "") || "home";
}

function validateStep(step) {
  if (step === 1) {
    orderData.name = document.getElementById("field-name")?.value.trim() || "";
    if (!orderData.name) {
      tg?.showAlert?.("Введите ваше имя");
      return false;
    }
  }
  if (step === 2) {
    orderData.phone = document.getElementById("field-phone")?.value.trim() || "";
    if (orderData.phone.length < 10) {
      tg?.showAlert?.("Укажите корректный телефон");
      return false;
    }
  }
  if (step === 3) {
    if (!orderData.date) {
      tg?.showAlert?.("Выберите дату");
      return false;
    }
    if (orderData.date === "Другая дата") {
      const custom = document.getElementById("field-custom-date")?.value.trim();
      if (!/^\d{2}\.\d{2}\.\d{4}$/.test(custom)) {
        tg?.showAlert?.("Введите дату в формате ДД.ММ.ГГГГ");
        return false;
      }
      orderData.date = custom;
    }
  }
  if (step === 4) {
    orderData.recipient = document.getElementById("field-recipient")?.value.trim() || "";
    if (!orderData.recipient) {
      tg?.showAlert?.("Введите имя получателя");
      return false;
    }
  }
  if (step === 5) {
    if (!orderData.occasion) {
      tg?.showAlert?.("Выберите повод");
      return false;
    }
    if (orderData.occasion === "Другое") {
      const c = document.getElementById("field-custom-occasion")?.value.trim();
      if (!c) {
        tg?.showAlert?.("Опишите повод");
        return false;
      }
      orderData.occasion = c;
    }
  }
  if (step === 6) {
    if (!orderData.relation) {
      tg?.showAlert?.("Укажите, кем приходится получатель");
      return false;
    }
    if (orderData.relation === "Другое") {
      const c = document.getElementById("field-custom-relation")?.value.trim();
      if (!c) {
        tg?.showAlert?.("Опишите связь с получателем");
        return false;
      }
      orderData.relation = c;
    }
    if (!orderData.budget) {
      tg?.showAlert?.("Выберите бюджет");
      return false;
    }
  }
  return true;
}

function nextStep() {
  if (!validateStep(currentStep)) return;
  if (currentStep < TOTAL_STEPS) showStep(currentStep + 1);
}

function prevStep() {
  if (currentStep > minStep()) showStep(currentStep - 1);
}

async function reset() {
  Object.keys(orderData).forEach((k) => {
    orderData[k] = "";
  });
  skipProfileSteps = false;
  document.querySelectorAll(".chip.selected, .budget-chip.selected").forEach((el) => {
    el.classList.remove("selected");
  });
  [
    "field-name",
    "field-phone",
    "field-recipient",
    "field-custom-date",
    "field-custom-occasion",
    "field-custom-relation",
  ].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.value = "";
  });
  document.querySelectorAll(".field-input.hidden").forEach((el) => el.classList.add("hidden"));

  const client = await fetchClientProfile();
  if (client?.name && client?.phone) {
    orderData.name = client.name;
    orderData.phone = client.phone;
    skipProfileSteps = true;
    showStep(3);
    return;
  }
  showStep(1);
}

document.querySelectorAll(".chips-wrap").forEach((wrap) => {
  wrap.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      wrap.querySelectorAll(".chip").forEach((c) => c.classList.remove("selected"));
      chip.classList.add("selected");
      const field = wrap.dataset.field;
      const value = chip.dataset.value;
      orderData[field] = value;

      if (field === "date") {
        document.getElementById("field-custom-date")?.classList.toggle(
          "hidden",
          value !== "Другая дата"
        );
      }
      if (field === "occasion") {
        document.getElementById("field-custom-occasion")?.classList.toggle(
          "hidden",
          value !== "Другое"
        );
      }
      if (field === "relation") {
        document.getElementById("field-custom-relation")?.classList.toggle(
          "hidden",
          value !== "Другое"
        );
      }
    });
  });
});

document.querySelectorAll(".budget-grid").forEach((grid) => {
  grid.querySelectorAll(".budget-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      grid.querySelectorAll(".budget-chip").forEach((c) => c.classList.remove("selected"));
      chip.classList.add("selected");
      orderData.budget = chip.dataset.value;
    });
  });
});

document.getElementById("btn-share-phone")?.addEventListener("click", () => {
  if (typeof tg?.requestContact === "function") {
    tg.requestContact((contact) => {
      if (contact?.phone_number) {
        let phone = contact.phone_number;
        if (!phone.startsWith("+")) phone = `+${phone}`;
        orderData.phone = phone;
        const input = document.getElementById("field-phone");
        if (input) input.value = phone;
      }
    });
    return;
  }
  tg?.showAlert?.("Введите номер вручную");
});

document.getElementById("btn-step-next")?.addEventListener("click", nextStep);
document.getElementById("btn-step-prev")?.addEventListener("click", prevStep);

function renderSummary() {
  const rows = [
    ["Получатель", orderData.recipient],
    ["Дата", orderData.date],
    ["Повод", orderData.occasion],
    ["Кто", orderData.relation],
    ["Бюджет", orderData.budget],
  ];
  document.getElementById("summary-rows").innerHTML = rows
    .map(
      ([k, v]) => `
    <div class="summary-row">
      <span class="summary-key">${k}</span>
      <span class="summary-val">${v}</span>
    </div>`
    )
    .join("");
}

document.getElementById("btn-submit")?.addEventListener("click", () => {
  if (!validateStep(TOTAL_STEPS)) return;

  document.getElementById("done-name").textContent = orderData.name;
  renderSummary();

  try {
    tg?.sendData(JSON.stringify(orderData));
  } catch (e) {
    console.error(e);
  }

  goTo("done");
});

window.VereskOrder = { reset, prevStep, nextStep, getStep: () => currentStep };
reset();
