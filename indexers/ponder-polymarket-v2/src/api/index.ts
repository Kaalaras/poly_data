import { Hono } from "hono";

const app = new Hono();

app.get("/poly-data/status", (c) => c.json({ ok: true }));

export default app;
