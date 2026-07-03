from app.xray.core import parse_x25519_output


def test_parse_x25519_output_legacy_format():
    output = "Private key: private-value\nPublic key: public-value\n"

    assert parse_x25519_output(output) == {
        "private_key": "private-value",
        "public_key": "public-value",
    }


def test_parse_x25519_output_new_xray_format():
    output = (
        "PrivateKey: private-value\n"
        "Password (PublicKey): public-value\n"
        "Hash32: hash-value\n"
    )

    assert parse_x25519_output(output) == {
        "private_key": "private-value",
        "public_key": "public-value",
    }
