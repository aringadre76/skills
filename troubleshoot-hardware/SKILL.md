---
name: troubleshoot-hardware
description: Use when investigating a recurring computer, display, audio, peripheral, or home-network malfunction or performance problem.
---

# Troubleshoot hardware

Diagnose from current observations, isolate likely causes, and make only changes the user has authorized.

## Establish the symptom

- Identify the affected device, host, symptom, trigger, and reproduction steps from the conversation and available evidence. Ask only when a missing detail changes the investigation.
- Read the relevant current device reference and any prior investigation. Treat old fixes, addresses, driver versions, and settings as hypotheses until checked on the affected system.
- Use current official technical documentation for unfamiliar or version-specific errors.
- Diagnosis authorizes inspection, not configuration edits. Before a disruptive action, confirm that the user authorized it and that it is necessary.

## Investigate

- Record the baseline and preserve a working control path. Change one variable at a time.
- Inspect current adapters, routes, device state, logs, and reproduction conditions as relevant. For network issues, test the intended interface, gateway, upstream access, DNS, and HTTPS separately.
- Separate observations from explanations. Use the next discriminating check when the cause remains uncertain.
- Apply the smallest supported reversible change when fixing is requested, and make rollback clear.

## Verify and report

Repeat the original failing operation under comparable conditions. Verify the affected path or device directly, along with a useful control. For intermittent symptoms, observe long enough to make the result meaningful.

Report what was observed, what changed, and whether the cause is confirmed, a workaround helped, or the symptom remains. A successful command or brief clean sample alone does not prove a lasting fix.
