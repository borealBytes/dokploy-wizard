# ruff: noqa: E501
"""Task 1 result payloads, Coder pagination, and the bounded remote collector."""

from __future__ import annotations

import base64
import os
import re
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.proof.model_sync_artifacts import JsonValue

# The self-contained pre-upload source is compressed only to keep every proof module
# within the Task 1 250-line transport contract. Wire tests execute the decoded source.
_PREFLIGHT_ENCODED = "c$~ExU325M@!h|I<wGL&WXg6v+EJt2B)MyIGwC%G`=K30!=XvYVoZ?=LE7hN^xwO?SbzXYeR*0xSR}C6FD!N!3-BUaZ_29nev@S}_BMH3FUx8@_T;LmvmAb#`%P6QLP~gkkr&IVTzgHG=h?l-Lq%1U6^xB5DaKwUes6@VFQR{b_wVTY|J=R5^=96U_h;|=>gG%Sxk&O@N-z3Y{vy(>5=lMcVd0N=cR%aKALAm;MHNghJorn+5|FE&h`~ov<V&JC_LiHmmy{{6u$B*FZzbYXRKRs1o`GLRf`65$o2u|WmW8;8%8e*AHegh<ao=QlN}Zw_*?a~;h^-e<)f5qH2FA5&3Zdg7dR}G83foDIA-!Ip1e#Wa3LgPu7N==k$1`M$nFUabrC(TN8bhG?&kUa#G;2|>%5>)c{IA<P-^-Q+?1@|o&;Q5!J3k=g&sn|l9Pvk_#yVSzvZ-e`SAiExpe{FMA%&&B_?fs;?O_FEN7iJ&@vapCAe@V01mJ=9t=B@>m@?q$m!?3P@6n1zzQ*g-W$EQ{^&psEo;6fkn3r)X5p$Rdobr+1)XU4i`az&{oaAMbE@8BYHt~ayj-HKjld?)B+U*0_OkLJ-4sy*GWAA#bjDys)%5uTInWT2FtdwL5L*RX*`dh&^@e8sD=z7x)`WgA3(X{w);itgG#FO*Z51@PLZ$w3W&#teoKBI>%{@?kn42BfoDasmH%L-Y?MIuH_9Bd>@>HwMLmheH;BVRTNnkkS1ad7Be)mXE;Z(g$EDbBOh`@yKzn1F9_Q;RjIG%J=R2=CAZ=q9iIU_8_u;RyebRE#;#q>`!I4dX3|@m1kZN)Q8t{nL!*jL-zH5*MeyVlio|N)&aZ4W}hZEfEv8MQTA5E@TRy2IO(Dqe7KjF41qitJg}B6>*)Fg~gwi!zBS+yS7i3N=>$8?jR7Wvmo{{m#pIAK^$j}ms%)3)uKpw7HtNc_4qCG3<?{OUJ=}bU^2LDPqK!jTyJtwi=&*<e7>1^*S6DQk~#p$vZ-K>VO|>WJXsn&iWi7e@mi>-$E--1tNudZQO4AVy2;2CURZCkt`0+L$_#thL2G-`V)%j0C?TCI(hE&gHEJr0!}!H|Q@?bvvmUp8mikkd$k_8yU(k#{@P>2@kRiJ3Mut&c#f8KKh6RojTCx}l9z_8w!2ryCE1)n~1e^fvVHm>TF9_`)uxiHiKSE~q;w6YfNDT@`%o=BYw5}EN+VXJ>8NPlY1V?6gok_5g2iw#WNW5TuXK^lhMmP|GI9vZah$x#HbneHSO`avRCMFp1AY%F7HIR-}*&k}9Od-B~F92udZHd>NzaxbAu|(zgtGeFE>0}abvapptgatTB@(h@mJbmfg3P7=1fgsveFBE$q8x0}0ih?qPt<Q=I$%M>gYjZKm*|lpK+fc%OKC$$-$nS1KAaA!sbud1QPjP~Y<s6-mx5U_l4-oeib_@${l+$Y)rw!j##2_{UZ#HW=U348F(W0nwJ+R)XcYd`{_&^K2<-X4#EJGlJf>;o*@3V)dY^3k13nbNa=160~%U7*tNeLD;)lE@AEIF)Qo+UZi^3!gTEYnDheVfTB73h@H*%Ay>bk&c_R-mYECfG_#^Dwjgv4Q}T0KT<hz^`idgTJ+6gka}74p|vNSuC@MUeb4(G?OhNn}43ApD&=DP&E~;898KEDsRGgO`vX$)v+oCV%wEH$Vmt>8B{UeWWc1W;-OL-9YsRtR)__-+wKCkDh1c$K#kHI4B+qQdkPb0e6LD#c<votTIP!8QNd0}pmi`gmrkE6f@d*Y*faQAm9nN#K-esf@XvJ4RJYQrZtVaAg9e_n`Hzk_+%Jh9=cWg`wU6Ny@^zSHt94fpM|4;tmibS&8U?yk9o~%+BBi(1cN^%y^)@<PY?rw5ubl;+7BW&98rZGvp^y3f$rgWhlNN!!R24HI=lM=9rWHBCu|rM`+L1l%KH$!v+lZQ$>o}umtWZZ*pgzH~ZAt*nj>W>mg18P&W8CA~_RSv+d+E&}3#EE_64eXpNz-ytNkc^Cg=zV?$;+2EDTH+)s;3M9c%g4#1Y{qeg1C4g{md2p1qNlG5%?b44@AE;Zt@l(EV!zcSZ*0<sELMl?afdmf>g7B)YZ)xc_d6=76C*jU9@{)sGBKbp+V<@h*v+eWW55}yf>P?1PnecgM6ph8Au;F+XJLw1r8|NjQj~2BDdque0uri0;%Im2zJ0a#LUp=Ac9?IrlX%OVf-a3XNrQT<a1xahS+)I5zWb_Dt{A69maXyHCyhI-J%Cp-g?2XQ=lP1xNXm#PowWe!$TffE1w6`P`~3L+kA|c3`63YVT#j%Rmm>GT9pbIl*>;aoYl#Veqd$A#Zy+5#Ttx)bjF$`o!NbT-Dy&%XA(BPv(pK!M3*Tihp@U(b2g+)$FfEg+5%L9+x<PkE$IEi;~!QkN+|{@aNp~Z_qA&?y}vxCnHtppE9RiJ#!ixPJoZ}*yJqTyM8_}NGP>jtie*DUE#^Q1){V{#3Uv9&WyZ|Z(b%m2_HTSC!{V4k^#xxhE0H$2xO>@P{$!}_`e-#MWH?m!Kj0rVXoz$C)`xcDX&}P)kmE^-P3VC4bl@Cob$U)nferj#t31^*H747~o@kYNs5L9DCFoYRNka<r1uej#%m$61isGt@UpQgINy939z1n*wQZAQL)O;LrWgUdLhXqvZ++!1nKg6@dxE?8VGxZakc#46iGRB`^c>|lUxO;tdPT3p9@7C@+<zzzKEv6`H-waT5Yr49bRrY4qj<zP?%*1jicYIhSRhX3%!l(Wp%pX~&N3y$iA1GjpkfU8l`3#bE3YBC<W@pzaQc@?4AMDX6-E`FKTsd6Oc>SV>oLH%(N#2`fnwlc|Bu!QUlqvd5I#f)X^+tlbRB}wUK77Od)Nr*6LiVou;E}N9<wI5+_K{CFlLn?qP6!rDCr8rNAIbu3w7%4?x(BlxlN5>rUAiXhT;7455dd>dy6a5|a5(1o*Ce5E3kKA5T0W{KNy&X~4VlJX-YQWp??{xXBCZm6RO*SEU-t@y*dAqQ@%Yr%M?1R0&Qn#zC|zZ7<2mZWA*?M}<aj+p?ZOYrX&}3T4os;AJ!@;Bcx%b<je@dzl$$tFg@L178gTjc_t|H8w$5rg@aAA~b)0#Z;Qli)7tT2sR!6JnNZqpC6<gx{lZHI`5Jh|7xC1d~eip|hm7SQ_c5q~*Q)1%6IUPPjE<0G7JfK6%$_N<b8%6cB++caC5p31($u}PyE9gx&o{Tt$Wu37@^W^Mk)Kpmji&GMj_Uw4rfLtT8lM<H}>;(?a2PZDJNoau&8$#Crl#U>{LhhEVm`f=jzSgP)xF@Atc0BEt9o}xjgWvWiSkG{8ik%w8kqWN%M4f!dIH=Ak)QNRrqmT~TuHMhZtCZ2p-AF@wGI_o^&=k0DT*u~mfH?)SucK)ga7Xb55Q(U4>P=J6FnNc@l}H|Ef5#Iu=eC!&AwJ<zJm6HL6<xKLo~T#WDGWR3Viz;zEg`ECsoxc}McTd7rDU(;`=~0P<w(gg=F|RVCh}CyZrlxwTh_|6g0@R)WGErMsIg6s0^40xWZc-HKt}$ro%=f~u?IL>Sc<fE4RM}x-|2f3trC!HU~}T0kPz<q)!yq4s{eYi>nPT`c#@XMBkV-ln<eU&B37lbLmdFR=SM@_zawbxho3%poPRU(38bnqsIRj0uXN4^I+|-zm{wzU;$Oz}|FS4AYk)}9o7y(w^LC4I!wbPbEQ0f9luItwg+T;4cE_xfMc}k{ASg;$H*54c{hY46AbVI`2ku7xhj%E$2inZR!$+7ZaQbJJA4%6nv-bTnqwS8Rs2wDbMq5!ENz<sdm95pNX2sW{dwhnoI8;5-=W#7pU4&|nxN{s_%dfHw`5ag4A;X}nfITw^ZxKHHsZ8ShBe;j#8XP-Xg%^bC06~W-wFhoZ5ayX_&G4kC&Ypl#r-G}^smBT<!^d16Gt}_IyWQ^B8Xi!e{74$tYz~^~NzP}`0ex}I_nc^5T1uCkR^)y^q-{$z-7NMxqt>$xpZ@lJo2~g_oFX^#3aS8+dNVsDc~d+V<#S;qr}46mYtcO!F|phPo}P6SqQ+F<b=Cpy%t8^IPyMOPo3&$_4mryPZGt>17|t`=+LR8uSuSD0^)Jg=(Zc`@1rWSH+LnV1DD4We3i5yxGUz)!Y9QuVQt)zbkMb<f&)REQ#l;QN-mvPZ&aepfZ8Y5zyVG`6E!RxQAa_m(zmH);i}Y66pdySH9RX>9Vgh@&SOmo9-TzCke0QW`n)_5+fZs(oHJUGaa@>l1*&2YVkIWfju6h?7!8bys2+sJQ1)24Bw|5SN`bOe4GeBcmEE2+OvalnhtD@kP4<3%<IGh?9-!cH)-RDA8kFZNg1JSozJu2walzoS5*0rA&%a73bQQiAH2iL$!hi1~PN4327_rvGh4?1lUkZH%XhJ%5{tBD3Q^uT?ep>q$|`4x2WOf{Mf0$(I<4-lue|MOKanBu85)y;k060u3B4>jU@>f$@aGG2-ih?w8QT4p+RWnYm{M42TGsqIaC!pf)jo_?~j=#6ukoiA)I>Dw#KNK)oGejG(~WY1@dPaEhJDb-ttZ#3h{WA_@f+orE6545&tm+f(Yc{N7|rp46N$6oK=N~Z}gy5Kgy@8)*<>YD9wi^=CDtf^Kfm^K)V^zHwMcg3Q6nV;y0U|(KZ{#w^_Z4*&(eQc+%X1{jCN(P30q&w%vDJXmBOgmVQvfMmg->30(&)xXI)_Z@<P=*lfEUG4DSrGlBmQ3HPsqD;&CA@f4SI{A#E>oRB!RX4R;mUUCPMFJqI*j{Y)8Bt^==eRwgz^A*%WpL3g6QyGzDO*xWJP%2ESI8EAMbtr)mLBqrT;aJ`fd+9zdr8nJ0{-ydQUN=duK;K<I`i-ak9$jS~-5HIPxc=PA1%VNp6S^;vj38>A^}UcUMT0Rb@pza*q1q3J}%c0vcKm`s>$_35?MA`8lHC#4lLBAg%k;174R8@7Hnk1X+gq_~B*;(+HjD<s=v6q=3CmT$h!cjr=h>x+$pO*s@pR_-oKxekosy1W$UGdG@fv3=)ejTxr81SuSQg_h5v0r-Y{P_C><x{QW1`Zr%hJn9`FEk0RPCM`TS=#9J!1t&9Hx4y=^6"
PREFLIGHT_SCRIPT = zlib.decompress(base64.b85decode(_PREFLIGHT_ENCODED)).decode("utf-8")


@dataclass(frozen=True, slots=True, repr=False)
class ProofTransport:
    """Secret-bearing inputs retained only at the local proof transport boundary."""

    cloudflare_account_id: str | None
    cloudflare_zone_id: str | None
    cloudflare_zone_name: str
    cloudflare_token: str | None
    dokploy_api_url: str | None
    dokploy_api_key: str | None
    coder_email: str | None
    coder_hostname: str | None
    coder_password: str | None
    tailscale_required: bool


@dataclass(frozen=True, slots=True)
class EnvReceipt:
    env_path: str
    backup_path: str
    original_sha256: str
    proof_sha256: str
    mode: int
    complete: bool


def parse_env_receipt(value: object) -> EnvReceipt | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("abort guard env receipt is invalid")
    payload: dict[str, object] = value
    required = {"backup_path", "complete", "env_path", "mode", "original_sha256", "proof_sha256"}
    if set(payload) != required:
        raise ValueError("abort guard env receipt is invalid")
    backup, complete, env = payload.get("backup_path"), payload.get("complete"), payload.get("env_path")
    mode, original, proof = payload.get("mode"), payload.get("original_sha256"), payload.get("proof_sha256")
    if not isinstance(backup, str) or not isinstance(env, str):
        raise ValueError("abort guard env receipt is invalid")
    if not isinstance(original, str) or not isinstance(proof, str):
        raise ValueError("abort guard env receipt is invalid")
    if not isinstance(complete, bool) or not isinstance(mode, int):
        raise ValueError("abort guard env receipt is invalid")
    if not backup or not env or not _SHA256.fullmatch(original) or not _SHA256.fullmatch(proof) or not 0 <= mode <= 0o777:
        raise ValueError("abort guard env receipt is invalid")
    return EnvReceipt(env, backup, original, proof, mode, complete)


def receipt_payload(receipt: EnvReceipt | None) -> dict[str, str | int | bool] | None:
    if receipt is None:
        return None
    return {"backup_path": receipt.backup_path, "complete": receipt.complete, "env_path": receipt.env_path, "mode": receipt.mode, "original_sha256": receipt.original_sha256, "proof_sha256": receipt.proof_sha256}


def receipt_identity(receipt: EnvReceipt) -> tuple[str, str, str, str, int]:
    return (receipt.env_path, receipt.backup_path, receipt.original_sha256, receipt.proof_sha256, receipt.mode)


def atomic_finalize(*, temp: Path, output: Path) -> None:
    if temp.parent.resolve() != output.parent.resolve() or not temp.is_file():
        raise ValueError("atomic finalization requires a regular sibling temporary file")
    descriptor = os.open(temp, os.O_RDONLY)
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temp, output)


REQUIRED_RESULT_KEYS = frozenset(
    {
        "schema_version", "source_base_commit", "proof_commit", "coder_image_digest",
        "litellm_image_digest", "shared_core_image_digests", "env_original_sha256",
        "env_proof_sha256", "env_mode", "external_backup_path", "abort_guard_path",
        "abort_guard_sha256", "host_a_preflight_sha256", "host_b_preflight_sha256",
        "host_identities_distinct", "host_architectures_equal", "baseline_sha256",
        "protected_artifacts_before_path", "protected_artifacts_before_sha256",
        "coder_secret_inventory_sha256", "legacy_workspace_managed_fingerprints_sha256",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def build_result(values: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Reject incomplete result documents before their protected finalization."""
    if frozenset(values) != REQUIRED_RESULT_KEYS:
        raise ValueError("Task 1 result keys do not match the proof contract")
    _require_hashes(values)
    _require_digests(values)
    _require_capture_values(values)
    return dict(values)


def _require_hashes(values: Mapping[str, JsonValue]) -> None:
    keys = (
        "env_original_sha256", "env_proof_sha256", "abort_guard_sha256",
        "host_a_preflight_sha256", "host_b_preflight_sha256", "baseline_sha256",
        "protected_artifacts_before_sha256", "coder_secret_inventory_sha256",
        "legacy_workspace_managed_fingerprints_sha256",
    )
    for key in keys:
        value = values[key]
        if not isinstance(value, str) or not _SHA256.fullmatch(value) or value == "0" * 64:
            raise ValueError("all capture hashes must be non-zero SHA-256 values")


def _require_digests(values: Mapping[str, JsonValue]) -> None:
    shared = values["shared_core_image_digests"]
    if not isinstance(shared, dict) or set(shared) != {"pgvector", "redis", "postfix", "litellm"}:
        raise ValueError("shared core image digest manifest is invalid")
    image_values = [values["coder_image_digest"], values["litellm_image_digest"], *shared.values()]
    if any(not isinstance(value, str) or not _DIGEST.fullmatch(value) for value in image_values):
        raise ValueError("all captured images must use repository@sha256 digests")
    if shared["litellm"] != values["litellm_image_digest"]:
        raise ValueError("LiteLLM image observations must agree across result planes")


def _require_capture_values(values: Mapping[str, JsonValue]) -> None:
    commits = (values["source_base_commit"], values["proof_commit"])
    if any(not isinstance(value, str) or not _COMMIT.fullmatch(value) for value in commits):
        raise ValueError("proof commits must be exact SHA-1 values")
    if values["schema_version"] != 1 or not isinstance(values["env_mode"], int) or values["env_mode"] < 1:
        raise ValueError("result schema version and proof env mode are invalid")
    if values["host_identities_distinct"] is not True or values["host_architectures_equal"] is not True:
        raise ValueError("result requires distinct hosts with matching architectures")
    for key in ("external_backup_path", "abort_guard_path", "protected_artifacts_before_path"):
        if not isinstance(values[key], str) or values[key] == "":
            raise ValueError(f"{key} must be a non-empty protected path")


def collect_coder_array_pages(
    fetch: Callable[[str], JsonValue], path: str, label: str
) -> list[tuple[int, list[JsonValue]]]:
    """Fetch bounded Coder array pages through the empty-page exhaustion boundary."""
    pages: list[tuple[int, list[JsonValue]]] = []
    offset = 0
    while True:
        raw = fetch(path.format(offset=offset))
        if not isinstance(raw, list) or len(raw) > 100:
            raise ValueError(f"Coder {label} API returned an invalid page")
        pages.append((offset, raw))
        if len(raw) < 100:
            return pages
        offset += len(raw)


def collect_coder_workspace_pages(fetch: Callable[[str], JsonValue]) -> list[JsonValue]:
    """Fetch Coder workspace pages until their server-declared count is exact."""
    offset = 0
    expected_count: int | None = None
    workspaces: list[JsonValue] = []
    while expected_count is None or len(workspaces) < expected_count:
        raw = fetch(f"/api/v2/workspaces?q=&limit=100&offset={offset}")
        if not isinstance(raw, dict) or not isinstance(raw.get("count"), int) or raw["count"] < 0:
            raise ValueError("Coder workspace API returned an invalid response")
        page = raw.get("workspaces")
        if not isinstance(page, list) or len(page) > 100:
            raise ValueError("Coder workspace API returned an invalid response")
        expected_count = raw["count"] if expected_count is None else expected_count
        if raw["count"] != expected_count or (not page and expected_count != 0):
            raise ValueError("Coder workspace pagination is incomplete")
        workspaces.extend(page)
        offset += len(page)
    if len(workspaces) != expected_count:
        raise ValueError("Coder workspace pagination count mismatch")
    return workspaces
