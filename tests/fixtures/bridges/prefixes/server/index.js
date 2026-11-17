// Express server: the users router lives in routes/users.js and is mounted at /api/users.
const express = require("express");
const users = require("./routes/users");

const app = express();
app.use(express.json());
app.use("/api/users", users);
app.listen(3000);
