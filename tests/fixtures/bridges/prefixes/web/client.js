// Browser calls to the FastAPI, Flask and Express servers above.

/** Read an item: /v1 (mount) + /items (router) + /{item_id}. */
export async function readItem(id) {
  const response = await fetch(`/v1/items/${id}`);
  return response.json();
}

/** Create an item with a literal path. */
export async function createItem(item) {
  const response = await fetch("/v1/items/", { method: "POST", body: JSON.stringify(item) });
  return response.json();
}

/** Not a route: /items without the /v1 mount prefix. */
export async function readItemWithoutPrefix(id) {
  const response = await fetch(`/items/${id}`);
  return response.json();
}

/** Orders: the router has no prefix of its own, the mount adds /v2. */
export async function listOrders() {
  const response = await fetch("/v2/orders");
  return response.json();
}

/** The shop blueprint keeps its url_prefix. */
export async function addToCart(item) {
  const response = await fetch("/shop/cart", { method: "POST", body: JSON.stringify(item) });
  return response.json();
}

/** admin is registered at /staff, so /staff/stats is the route ... */
export async function staffStats() {
  const response = await fetch("/staff/stats");
  return response.json();
}

/** ... and /admin/stats (its own url_prefix) is not. */
export async function adminStats() {
  const response = await fetch("/admin/stats");
  return response.json();
}

/** Express: /api/users (app.use) + /:id (router). */
export async function getUser(id) {
  const response = await fetch(`/api/users/${id}`);
  return response.json();
}
