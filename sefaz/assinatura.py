def assinar_xml(xml_str: str, cert_file: str, key_file: str) -> str:
    """Assina XML com certificado digital"""
    try:
        from signxml import XMLSigner, methods
        from lxml import etree
        import re

        root = etree.fromstring(xml_str.encode("utf-8"))
        signer = XMLSigner(method=methods.enveloped, signature_algorithm="rsa-sha1", digest_algorithm="sha1")

        with open(cert_file, "rb") as cf, open(key_file, "rb") as kf:
            cert_data = cf.read()
            key_data = kf.read()

        signed = signer.sign(root, key=key_data, cert=cert_data)
        return etree.tostring(signed, encoding="unicode")
    except Exception as e:
        return xml_str

def assinar_xml_evento(xml_str: str, cert_file: str, key_file: str) -> str:
    return assinar_xml(xml_str, cert_file, key_file)
