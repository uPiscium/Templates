# Model availability

Agent Core assigns one fixed GPT-5.6 model to each configured role. The configured model is authoritative for that role.

Agent Core does not substitute another model, invoke a fallback agent, or retry the same bounded objective under another model when provider execution is unavailable. The affected Task or Work Unit returns `BLOCKED`, preserves relevant evidence, and reports the exact provider/model failure.

This contract preserves role quality and authority; it does not guarantee provider or model availability.
