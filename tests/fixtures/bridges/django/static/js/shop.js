// Browser code for the shop: jQuery and XMLHttpRequest clients.

/** Refresh the item list with jQuery's $.get. */
function loadItems(render) {
  return $.get("/shop/items/").then((data) => render(data.items));
}

/** Dynamic: the id is a variable, so this must not be matched. */
function saveItem(id, fields) {
  return $.ajax({ url: "/shop/items/" + id + "/edit/", type: "POST", data: fields });
}

/** Create an order: $.ajax with an options object. */
function createOrder(order) {
  return $.ajax({ url: "/api/orders/", type: "POST", data: JSON.stringify(order) });
}

/** Read order 42 with a plain XMLHttpRequest. */
function readOrder(onLoad) {
  const xhr = new XMLHttpRequest();
  xhr.open("GET", "/api/orders/42/");
  xhr.onload = () => onLoad(JSON.parse(xhr.responseText));
  xhr.send();
  return xhr;
}

/** Post to the legacy page; window.open below is not a request. */
function legacy(popup) {
  if (popup) window.open("/shop/items/", "_blank");
  return $.post("/legacy/items/9/", { from: "shop.js" });
}
