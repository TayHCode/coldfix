---
id: model-routing
title: Route models by role and the owner's latest instruction
proof: judged
---
# Route models by role and the owner's latest instruction

Choose provider, model and effort from the live role in the model registry, subject to the owner's latest explicit instruction. Resolve the configured building role for the task, an independent review role that excludes every builder, and a bulk role for routine bulk work. Pass a complete explicit target; never inherit a model or use auto. A reviewer must differ from every builder. An unavailable configured route blocks that dispatch; never silently substitute. Apply a count limit only when the owner explicitly sets one.

## Applies when

Any orchestrator or coordinator that spawns subagents, builders, auditors or loops.

## Reproduce

The owner names the models to use, and the orchestrator keeps dispatching its most expensive default, because no dispatch step checks the chosen model against the routing or the authorization.

## Fix

Spawn all subagents through one dispatch helper that takes a role, resolves provider, model and effort from the registry at call time, and refuses an explicit model outside that mapping unless an override with a recorded owner authorization (scope and count) is passed. Record owner routing instructions in the registry, not in chat memory.

## Verify

Resolve the bulk role and compare provider, model and effort with the registry. Resolve the review role while excluding every builder model, and assert none of them is returned. An unknown role and a registry with no independent reviewer must both fail.

## Encode it higher

Resolve roles at the dispatch boundary, validate them against the host's capability catalog, keep model identities in the registry rather than in instructions, and fail closed when no independent route is available.
