/**
 * Who is signed in.
 *
 * For now this is a demo sign-in: any name and password are accepted and only
 * the name is kept, in this browser. It protects nothing; the server doesn't
 * know about it. GitHub sign-in will replace `signIn`: the button sends the
 * visitor to the server (/api/auth/github), which completes OAuth and sets a
 * session cookie; `currentUser` will then ask the server (/api/auth/me) instead
 * of reading localStorage. Pages only use the functions below, so they won't change.
 */

const KEY = "coderag.user";

// The demo repositories (sample, requests, click) are open to everyone. Signing in is
// needed to add a repository and to see the ones you added (app.js asks currentUser()).

/** GitHub sign-in isn't wired up yet; the button says so. */
export const GITHUB_READY = false;

/** {name, provider, since} or null. */
export function currentUser() {
  try {
    const user = JSON.parse(localStorage.getItem(KEY) || "null");
    return user && typeof user.name === "string" && user.name.trim() ? user : null;
  } catch {
    return null;
  }
}

/** Demo sign-in: any non-empty name and password. The password is never stored. */
export function signIn(name, password) {
  name = (name || "").trim();
  if (!name) return { ok: false, error: "Enter a name or email." };
  if (!password) return { ok: false, error: "Enter a password (any password works for now)." };
  const user = { name: name.slice(0, 80), provider: "demo", since: new Date().toISOString() };
  try {
    localStorage.setItem(KEY, JSON.stringify(user));
  } catch {
    return { ok: false, error: "This browser is blocking storage, so sign-in can't be remembered." };
  }
  return { ok: true, user };
}

export function signOut() {
  try { localStorage.removeItem(KEY); } catch { /* nothing kept */ }
}

/** Where to go after signing in: a path on this site only (never another origin). */
export function safeNext(raw, fallback = "/app") {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.includes("\\")) return fallback;
  return raw;
}

/** Two letters for the avatar: "Ada Lovelace" -> "AL", "ada@x.io" -> "AD". */
export function initials(name) {
  const words = name.replace(/@.*/, "").split(/[\s._-]+/).filter(Boolean);
  const letters = words.length > 1 ? words[0][0] + words[1][0] : (words[0] || "?").slice(0, 2);
  return letters.toUpperCase();
}
