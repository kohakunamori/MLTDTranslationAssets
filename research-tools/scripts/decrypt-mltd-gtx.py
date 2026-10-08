"""Read-only decryptor/inspector for MLTD JP 9.0.100 GTX TextAsset payloads.

Recovered from exact 9.0.100 Imas.TextManager:
  PBKDF2-HMAC-SHA1(password=b"Millicon", salt=b"DAISUL___", iterations=1000)
  Rijndael/AES CBC, KeySize=192, BlockSize=128, PKCS7
  PBKDF2 stream is consumed as 24-byte key then 16-byte IV.

The tool does not modify bundles and does not dump full plaintext unless --output is given.
"""
from __future__ import annotations
import argparse, hashlib, json, re
from pathlib import Path
import UnityPy
from Crypto.Cipher import AES

PASSWORD=b"Millicon"
SALT=b"DAISUL___"
ITERATIONS=1000

def decrypt_bundle(path: Path) -> tuple[bytes, bytes, str]:
    env=UnityPy.load(str(path))
    assets=[o.read() for o in env.objects if o.type.name=="TextAsset"]
    if len(assets)!=1:
        raise ValueError(f"expected exactly one TextAsset, got {len(assets)}")
    name=assets[0].m_Name
    raw=assets[0].m_Script
    if isinstance(raw,str): raw=raw.encode("utf-8")
    cipher=bytes(raw)
    if len(cipher)%16:
        raise ValueError("cipher payload is not AES block aligned")
    dk=hashlib.pbkdf2_hmac("sha1",PASSWORD,SALT,ITERATIONS,dklen=40)
    plain=AES.new(dk[:24],AES.MODE_CBC,dk[24:]).decrypt(cipher)
    pad=plain[-1]
    if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad])*pad:
        raise ValueError("invalid PKCS7 padding")
    return cipher, plain[:-pad], name

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--match", action="append", default=[], help="regex applied to key names")
    ap.add_argument("--output", type=Path, help="optional full decrypted UTF-8 output")
    ap.add_argument("--json", action="store_true")
    ns=ap.parse_args()
    cipher,plain,name=decrypt_bundle(ns.bundle)
    text=plain.decode("utf-8")
    records=[]
    for rec in text.split("|"):
        if "^" in rec:
            k,v=rec.split("^",1); records.append((k,v))
    selected=[]
    regexes=[re.compile(x,re.I) for x in ns.match]
    if regexes:
        selected=[{"key":k,"value":v} for k,v in records if any(rx.search(k) for rx in regexes)]
    if ns.output:
        ns.output.write_bytes(plain)
    result={
      "bundle":str(ns.bundle), "text_asset":name,
      "bundle_sha256":hashlib.sha256(ns.bundle.read_bytes()).hexdigest(),
      "cipher_sha256":hashlib.sha256(cipher).hexdigest(),
      "plain_sha256":hashlib.sha256(plain).hexdigest(),
      "cipher_bytes":len(cipher), "plain_bytes":len(plain),
      "record_count":len(records), "unique_key_count":len({k for k,_ in records}),
      "matches":selected,
    }
    if ns.json: print(json.dumps(result,ensure_ascii=False,indent=2))
    else:
        for k,v in result.items():
            if k!="matches": print(f"{k}: {v}")
        for x in selected: print(f"{x['key']}={x['value']!r}")
if __name__=="__main__": main()
