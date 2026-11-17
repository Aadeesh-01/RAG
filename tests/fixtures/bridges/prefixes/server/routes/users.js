// Users, as an Express router: its routes are relative to where it is mounted.
const express = require("express");
const router = express.Router();

/** One user by id, with their display name. */
function getUser(req, res) {
  const user = { id: req.params.id, name: `user ${req.params.id}`, active: true };
  res.json(user);
}

router.get("/:id", getUser);

module.exports = router;
