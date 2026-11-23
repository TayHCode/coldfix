---
id: authz-everywhere
title: Authorize every privileged action against identity and owner
proof: rerunnable
scope: public
category: security
rank: 1
featured: true
---
# Authorize every privileged action against identity and owner

Bind every privileged action to the authenticated identity and the resource owner or tenant. Path-prefix middleware checks (`startsWith`) leave routes open. Check cross-origin submissions, and check scope on error and replay paths too.

## Applies when

Middleware, API routes, auth and session code, and gateway servers.

## Reproduce

Call the route with another tenant's id, with a path that merely starts with the protected or public prefix, with a cross-origin form post, and through the error or replay path.

## Fix

Bind each privileged action to the authenticated identity and resource owner or tenant. Match paths exactly or by whole segment, not with `startsWith`. Check origin and scope on error and replay paths too.

## Verify

Add negative tests per route: wrong tenant, prefix bypass, cross-origin and replay. Each must return 401 or 403.

## Encode it higher

Architecture: one `authorize()` wrapper every route must go through, and a CI route-inventory check that fails on a route without it.
