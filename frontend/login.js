import { currentUser, safeNext, signOut } from "./auth.js";

const $ = (s) => document.querySelector(s);
const params = new URLSearchParams(window.location.search);
const next = safeNext(params.get("next"));
const form = $("#login-form");
const error = $("#login-error");

// the server sends people back here with ?error=<code> when sign-in didn't finish
const ERRORS = {
  unavailable: "GitHub sign-in isn't set up on this server yet.",
  denied: "Sign-in was cancelled on GitHub.",
  state: "That sign-in attempt expired or didn't match. Please try again.",
  github: "GitHub didn't complete the sign-in. Please try again.",
};

// came from a link into the app: say where they'll land
if (next !== "/app") $("#login-why").textContent = "to continue to coderag.";

function showSignedIn(user) {
  $("#signed-in").hidden = !user;
  form.hidden = Boolean(user);
  if (!user) return;
  $("#signed-in-name").textContent = user.name;
  $("#signed-in-login").textContent = user.login && user.login !== user.name ? ` (@${user.login})` : "";
  $("#continue").href = next;
  $("#continue").focus();
}

function showError(code) {
  const message = code ? ERRORS[code] || ERRORS.github : "";
  error.textContent = message;
  error.hidden = !message;
}

// a plain link, so it works before (or without) this script; here it only learns where to return
$("#github").href = `/api/auth/github?next=${encodeURIComponent(next)}`;

$("#switch").addEventListener("click", async () => {
  await signOut();
  showSignedIn(null);
  $("#github").focus();
});

showError(params.get("error"));
showSignedIn(currentUser());
if (!currentUser()) $("#github").focus();
