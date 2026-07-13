# Composition Behavior Contract v1 fixture lock

This is a verified copy of the OS-independent scenarios from Grimodex's
`ime-contract/composition-behavior-v1`. `contract-lock.json` pins every copied
file by SHA-256 so Linux tests never depend on a developer's adjacent checkout.

When the contract changes, update the source contract first, copy the changed
scenario files, update the hashes, and make the Linux adapter test pass in the
same change.
