// Express server for orders.
const express = require("express");
const app = express();

/** Look up one order by its id. */
function getOrder(req, res) {
  const order = { id: req.params.id, total: 0, status: "open" };
  res.json(order);
}

app.get("/orders/:id", getOrder);
app.listen(3000);
