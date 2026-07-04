/** Feature toggles — persisted in localStorage, applied via data-feature attributes. */

const FEATURE_STORAGE_KEY = "guide_feature_settings";

const FEATURE_DEFAULTS = {
  gps_navigation: true,
  navigator: true,
  audio_test: true,
  direct_robot_chat: true,
};

function loadFeatureSettings() {
  try {
    const raw = localStorage.getItem(FEATURE_STORAGE_KEY);
    if (!raw) return { ...FEATURE_DEFAULTS };
    const parsed = JSON.parse(raw);
    return { ...FEATURE_DEFAULTS, ...parsed };
  } catch {
    return { ...FEATURE_DEFAULTS };
  }
}

function saveFeatureSettings(settings) {
  const merged = { ...FEATURE_DEFAULTS, ...settings };
  localStorage.setItem(FEATURE_STORAGE_KEY, JSON.stringify(merged));
  return merged;
}

function isFeatureEnabled(key) {
  return Boolean(loadFeatureSettings()[key]);
}

function applyFeatureVisibility(root = document) {
  const settings = loadFeatureSettings();
  root.querySelectorAll("[data-feature]").forEach((el) => {
    const key = el.getAttribute("data-feature");
    if (!key || !(key in FEATURE_DEFAULTS)) return;
    el.hidden = !settings[key];
  });
}

function guardFeaturePage(key, redirectTo = "/") {
  if (isFeatureEnabled(key)) return;
  window.location.replace(redirectTo);
}

function initFeatureSettingsPage() {
  const form = document.getElementById("feature-settings-form");
  if (!form) return;

  const settings = loadFeatureSettings();
  Object.keys(FEATURE_DEFAULTS).forEach((key) => {
    const input = form.querySelector(`[name="${key}"]`);
    if (input) input.checked = settings[key];
  });

  form.addEventListener("change", () => {
    const next = {};
    Object.keys(FEATURE_DEFAULTS).forEach((key) => {
      const input = form.querySelector(`[name="${key}"]`);
      next[key] = input ? input.checked : FEATURE_DEFAULTS[key];
    });
    saveFeatureSettings(next);
    applyFeatureVisibility();
  });

  const resetBtn = document.getElementById("feature-settings-reset");
  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      saveFeatureSettings({ ...FEATURE_DEFAULTS });
      Object.keys(FEATURE_DEFAULTS).forEach((key) => {
        const input = form.querySelector(`[name="${key}"]`);
        if (input) input.checked = FEATURE_DEFAULTS[key];
      });
      applyFeatureVisibility();
    });
  }
}
