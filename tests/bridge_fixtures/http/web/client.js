// The browser side of the repository API.

async function fetchJSON(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${response.status} ${path}`);
  return response.json();
}

/** Index a repository: POST /api/repos. */
export async function addRepo(url) {
  const response = await fetch("/api/repos", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  return response.json();
}

/** The signed-in user's repositories, through the fetchJSON wrapper and a "+" of literals. */
export async function loadMine() {
  const mine = await fetchJSON("/api/repos/" + "mine");
  return mine.map((repo) => repo.id);
}

/** Dynamic URL: a template with a value. Must not be matched to any route. */
export async function openRepo(id) {
  const response = await fetch(`/api/repos/${encodeURIComponent(id)}/ask`, { method: "POST" });
  return response.json();
}

/** Dynamic URL: a literal plus a variable. Must not be matched either. */
export function forgetRepo(id) {
  return axios.delete("/api/repos/mine/" + id).then((response) => response.status === 204);
}

/** Absolute URL to the health check, through axios. */
export function health() {
  return axios.get("http://localhost:7860/api/health?verbose=1").then((response) => response.data.ok);
}
