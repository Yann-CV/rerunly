"use strict";

const form = document.getElementById("login-form");
const submit = document.getElementById("login-submit");
const errorBox = document.getElementById("login-error");
const password = document.getElementById("password");
const confirmPassword = document.getElementById("confirm-password");
const recoveryCode = document.getElementById("recovery-code");
let creating = false;

const openDashboard = () => location.replace("/" + location.hash);
const showError = (message) => {
  errorBox.textContent = message;
  errorBox.hidden = false;
};

async function initializeLogin() {
  try {
    const response = await fetch("/auth/status", {cache: "no-store"});
    if (!response.ok) throw new Error("Could not check your local account. Reload to retry.");
    const status = await response.json();
    if (status.signed_in) { openDashboard(); return; }
    creating = !status.configured;
    if (creating && !status.can_setup) {
      document.getElementById("login-description").textContent = "Create the password on the machine running Rerunly first. Open its local dashboard or run: uv run rerunly password";
      return;
    }
    document.getElementById("login-title").textContent = creating ? "Create your password" : "Welcome back";
    document.getElementById("login-description").textContent = creating
      ? "Protect your history and reruns with one local password. Use at least 12 characters."
      : "Enter your local password to open your history and reruns.";
    document.getElementById("login-help").textContent = creating
      ? "One account, stored on this machine. No email or online account."
      : "Forgot your password? Use your saved recovery code with: uv run rerunly password --recover";
    password.autocomplete = creating ? "new-password" : "current-password";
    if (creating) password.minLength = 12;
    document.getElementById("confirm-field").hidden = !creating;
    confirmPassword.required = creating;
    submit.textContent = creating ? "Create password" : "Sign in";
    form.hidden = false;
    password.focus();
  } catch (error) { showError(error.message); }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.hidden = true;
  if (creating && password.value !== confirmPassword.value) {
    showError("The passwords do not match.");
    confirmPassword.focus();
    return;
  }
  submit.disabled = true;
  try {
    const response = await fetch(creating ? "/auth/setup" : "/auth/login", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({password: password.value, ...(creating ? {confirm_password: confirmPassword.value} : {})}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not sign in. Try again.");
    password.value = "";
    confirmPassword.value = "";
    if (creating) {
      form.hidden = true;
      document.getElementById("login-title").textContent = "Save your recovery code";
      document.getElementById("login-description").textContent = "You'll need this code if you forget your password. It is shown only once.";
      document.getElementById("login-help").hidden = true;
      recoveryCode.value = data.recovery_code;
      document.getElementById("recovery-panel").hidden = false;
      document.getElementById("recovery-done").focus();
    } else openDashboard();
  } catch (error) { showError(error.message); }
  finally { submit.disabled = false; }
});

document.getElementById("copy-recovery").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  try {
    await navigator.clipboard.writeText(recoveryCode.value);
    button.textContent = "Copied";
  } catch {
    recoveryCode.focus();
    recoveryCode.select();
    button.textContent = "Select and copy the code";
  }
});
document.getElementById("recovery-done").addEventListener("click", () => {
  recoveryCode.value = "";
  openDashboard();
});

window.addEventListener("pageshow", (event) => { if (event.persisted) location.reload(); });
initializeLogin();
