import { GITHUB_READY, currentUser, safeNext, signIn, signOut } from "./auth.js";

const $ = (s) => document.querySelector(s);
const next = safeNext(new URLSearchParams(window.location.search).get("next"));
const form = $("#login-form");
const error = $("#login-error");

// came from a link into the app: say where they'll land
if (next !== "/app") $("#login-why").textContent = "to continue to coderag.";

function showSignedIn(user) {
  $("#signed-in").hidden = !user;
  form.hidden = Boolean(user);
  if (!user) return;
  $("#signed-in-name").textContent = user.name;
  $("#continue").href = next;
  $("#continue").focus();
}

function showError(message) {
  error.textContent = message || "";
  error.hidden = !message;
  for (const input of [$("#login-name"), $("#login-password")]) {
    if (message) input.setAttribute("aria-invalid", String(!input.value.trim()));
    else input.removeAttribute("aria-invalid");
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const result = signIn($("#login-name").value, $("#login-password").value);
  if (!result.ok) {
    showError(result.error);
    (!$("#login-name").value.trim() ? $("#login-name") : $("#login-password")).focus();
    return;
  }
  window.location.replace(next);
});
for (const input of form.querySelectorAll("input")) input.addEventListener("input", () => showError(""));

$("#switch").addEventListener("click", () => {
  signOut();
  showSignedIn(null);
  $("#login-name").focus();
});

// GitHub sign-in: the button is ready for it; the server side isn't yet
$("#github").disabled = !GITHUB_READY;

showSignedIn(currentUser());
if (!currentUser()) $("#login-name").focus();
