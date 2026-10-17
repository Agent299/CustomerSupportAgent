# ADR 0013: Passwordless email-code authentication, built in-house

- Status: Accepted
- Date: 2026-10-17

## Context

Customers must sign in with email verification; agents need a login too.

## Decision

6-digit code via Resend, hashed in Key Value (10 min TTL, 5 attempts), Turnstile on the form; short-lived JWT plus rotating refresh token in an httpOnly cookie; agents are users with `role=agent`.

## Consequences

One small auth system to own and test. UI and API must share a custom domain for cookies.
