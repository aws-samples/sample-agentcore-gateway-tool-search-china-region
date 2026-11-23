# Synthetic SDK compatibility model

`reviewed_control_plane_session` in [conftest.py](../../conftest.py) copies the
service model from the locked, installed public botocore package into a test
temporary directory and removes `listingMode` and `mcpToolSchema` from its parser.
This deliberately constructed model tests field-loss recovery; it is not a
captured SDK override or evidence of a particular deployed service version.

Separate tests use the unmodified installed SDK model. Neither path reads user
model overrides or requires AWS credentials or network access.
