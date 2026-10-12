---
title: Nothing retries forever
paths: ["app/**"]
severity: high
---

Calls to suppliers and the payments API have a timeout. A failure is logged
and the caller decides what to do. A retry loop must stop after a few tries
and must not retry errors that will never succeed (404, 401, a bad request).

Bad: `while True:` around a call, retrying every error.

Good: at most three tries with a pause, only for timeouts and 5xx answers.
