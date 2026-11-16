/**
 * Who is signed in.
 *
 * Sign-in is GitHub OAuth, done by the server: `signIn` sends the visitor to
 * /api/auth/github, which completes OAuth and sets an HttpOnly session cookie
 * (this page never sees it, or the GitHub token). `currentUser` is what
 * /api/auth/me answered: this module asks once when it loads (top-level await),
 * so every page that imports it starts with the answer and `currentUser` stays
 * synchronous. This module is data only; pages draw the avatar themselves
 * (initials and avatarSrc in lib.js).
 */

const OLD_DEMO_KEY = "coderag.user";     // the old demo sign-in kept a name here

// The demo repositories (sample, requests, click) are open to everyone. Signing in is
// needed to add a repository and to see the ones you added (app.js asks currentUser()).

export const GITHUB_READY = true;

async function fetchMe() {
  try {
    const r = await fetch("/api/auth/me", { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (!r.ok) return null;
    const me = await r.json();
    if (!me || typeof me.login !== "string" || !me.login) return null;
    return { name: me.name || me.login, login: me.login, avatar_url: me.avatar_url || "", provider: "github" };
  } catch {
    return null;              // offline, or not served by coderag (e.g. unit tests)
  }
}

try { globalThis.localStorage?.removeItem(OLD_DEMO_KEY); } catch { /* storage blocked */ }
let user = await fetchMe();

/** {name, login, avatar_url, provider: "github"} or null. */
export function currentUser() {
  return user;
}

/** Go to GitHub to sign in; come back to `next` (a path on this site). */
export function signIn(next = "/app") {
  window.location.assign(`/api/auth/github?next=${encodeURIComponent(safeNext(next))}`);
}

/** Forget the user here at once and end the server session. Resolves when the server has. */
export function signOut() {
  user = null;
  return fetch("/api/auth/logout", { method: "POST", credentials: "same-origin", keepalive: true })
    .catch(() => {});
}

/** Where to go after signing in: a path on this site only (never another origin). */
export function safeNext(raw, fallback = "/app") {
  if (typeof raw !== "string" || !raw.startsWith("/") || raw.startsWith("//") || /[\\\x00-\x1f\x7f]/.test(raw)) {
    return fallback;
  }
  return raw;
}
