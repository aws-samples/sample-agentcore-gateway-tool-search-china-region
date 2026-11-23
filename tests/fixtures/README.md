# Test inputs

These fixtures contain synthetic protocol and configuration inputs, packaging
smoke programs, and no deployed account snapshots or captured model responses.

- `service/`: hand-authored Gateway event and Search metadata examples. They
  exercise the supported application contract, not a claim of live service validation.
- `sdk-models/`: explains the compatibility model constructed in test temporary storage.
- Infrastructure and provider tests construct isolated responses and templates.

Ordinary tests block network access. Live integration requires both explicit
switches documented in the developer guide and separately supplied inputs.
