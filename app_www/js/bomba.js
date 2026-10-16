// ============================================================
// App Postos SN — botão da BOMBA (05/10/2026)
// ------------------------------------------------------------
// Ler o QR do bico (ou digitar o número) → escolher como paga → o caixa já sabe quem
// é o cliente: a compra vira pontos (1 por R$ 1) e, onde o posto tem cashback, Pix de
// volta. A prazo exige a foto do rosto (assinatura) — mesma regra do portal.
// QR: BarcodeDetector (Android) ou jsQR (iPhone). Sempre dá para digitar.
// ============================================================

let _cam = null, _camTimer = null, _vivoTimer = null;

function pararCamera() {
  if (_camTimer) { clearInterval(_camTimer); _camTimer = null; }
  if (_cam) { _cam.getTracks().forEach(t => t.stop()); _cam = null; }
  const c = document.getElementById("camera");
  if (c) c.remove();
}
function pararVivo() { if (_vivoTimer) { clearInterval(_vivoTimer); _vivoTimer = null; } }

// "https://…/cashback?p=<posto>&bico=3" ou só o número
function lerQr(texto) {
  try {
    const u = new URL(texto);
    const p = u.searchParams.get("p"), b = u.searchParams.get("bico");
    if (p && /^[0-9a-f-]{36}$/i.test(p)) { S.posto = p; LS.set("psn_posto", p); }
    if (b) return soDigitos(b);
  } catch (e) { /* não é URL */ }
  const so = soDigitos(texto);
  return so.length >= 1 && so.length <= 3 ? so : null;
}

async function _carregarJsqr() {
  if (window.jsQR) return;
  await new Promise((ok, err) => {
    const s = document.createElement("script");
    s.src = "js/jsqr.min.js"; s.onload = ok; s.onerror = err;
    document.head.appendChild(s);
  });
}

async function telaBomba() {
  el().innerHTML = "";
  const c = document.createElement("div");
  c.className = "camera"; c.id = "camera";
  c.innerHTML = `<button class="fechar" onclick="history.back()" aria-label="Voltar">${ICO.voltar(22)}</button>
    <video id="cam-video" playsinline muted autoplay></video><div class="mira"></div>
    <div class="dica">${ICO.qr(18)} Aponte para o código da bomba</div>
    <div id="cam-msg" style="margin-top:10px;font-size:.85rem;opacity:.8;text-align:center"></div>
    <button class="btn" style="position:absolute;bottom:calc(env(safe-area-inset-bottom) + 24px);left:20px;right:20px;width:auto;margin:0 auto"
      onclick="pararCamera();ir('#/acionar')">${ICO.teclado(18)} &nbsp;Digitar código</button>`;
  document.body.appendChild(c);
  const msg = document.getElementById("cam-msg"), vid = document.getElementById("cam-video");
  try {
    _cam = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false });
    vid.srcObject = _cam;
    try { await vid.play(); } catch (e) { /* alguns aparelhos já tocam sozinhos */ }
  } catch (e) {
    msg.textContent = e && e.name === "NotAllowedError" ? "Sem permissão para a câmera. Toque em Digitar código." : "A câmera não abriu. Toque em Digitar código.";
    return;
  }
  const achou = txt => {
    const b = lerQr(txt);
    if (!b) return false;
    if (navigator.vibrate) navigator.vibrate(60);
    S.bico = b;
    pararCamera();
    ir("#/acionar");
    return true;
  };
  if ("BarcodeDetector" in window) {
    const det = new BarcodeDetector({ formats: ["qr_code"] });
    _camTimer = setInterval(async () => {
      try { const cs = await det.detect(vid); if (cs.length) achou(cs[0].rawValue); } catch (e) { /* quadro ruim */ }
    }, 300);
  } else {
    try { await _carregarJsqr(); } catch (e) { msg.textContent = "Leitor indisponível. Toque em Digitar código."; return; }
    const cv = document.createElement("canvas"), cx = cv.getContext("2d", { willReadFrequently: true });
    _camTimer = setInterval(() => {
      if (!vid.videoWidth) return;
      const esc = Math.min(1, 640 / Math.max(vid.videoWidth, vid.videoHeight));
      cv.width = Math.round(vid.videoWidth * esc); cv.height = Math.round(vid.videoHeight * esc);
      cx.drawImage(vid, 0, 0, cv.width, cv.height);
      const img = cx.getImageData(0, 0, cv.width, cv.height);
      const q = window.jsQR(img.data, img.width, img.height);
      if (q && q.data) achou(q.data);
    }, 350);
  }
}

// ---------------------------------------------------------------- acionar
const COMBS = ["GASOLINA COMUM", "GASOLINA ADITIVADA", "ETANOL", "DIESEL S10", "DIESEL S500"];

async function telaAcionar() {
  const postos = await carregarPostos();
  if (!S.posto && S.me && S.me.posto_favorito) S.posto = S.me.posto_favorito;
  el().innerHTML = `<div class="tela">${topoSimples("Vou abastecer", true)}
    <div class="form">
      <div class="campo"><label>Posto</label><select id="a-posto"><option value="">Escolha o posto…</option>${postos.map(p =>
        `<option value="${esc(p.id)}" ${p.id === S.posto ? "selected" : ""}>${esc(p.nome)} · ${esc(p.cidade || "")}</option>`).join("")}</select></div>
      <div class="duas">
        <div class="campo"><label>Número do bico</label><input id="a-bico" inputmode="numeric" maxlength="3" value="${esc(S.bico || "")}" placeholder="ex.: 3"></div>
        <div class="campo"><label>Combustível</label><select id="a-comb"><option value="">—</option>${COMBS.map(c => `<option>${c}</option>`).join("")}</select></div>
      </div>
      <div id="a-info" class="vazio" style="padding:6px 2px 0"></div>
      <div id="a-modo"></div>
      <div class="campo" id="a-forma-campo"><label>Como vai pagar</label><select id="a-forma"><option value="17">PIX</option><option value="01">Dinheiro</option></select></div>
      <div class="campo" id="a-forma-desc-campo" style="display:none"><label>Como vai pagar</label><select id="a-forma-desc"></select></div>
      <div id="a-prazo-msg" class="vazio" style="padding:6px 2px 0"></div>
      <button class="btn" id="a-btn">Avisar o caixa</button>
      <div class="msg" id="a-msg"></div>
      <div class="vazio" id="a-rodape" style="text-align:center">O caixa recebe o seu nome na hora. A compra vale <b>1 ponto a cada R$ 1</b>.</div>
    </div></div>`;
  const $ = id => document.getElementById(id);
  // DESCONTO DO APP NA BOMBA (16/10/2026): bico com desconto na tabela DESCONTO APP do posto ganha a escolha
  // "Desconto agora" (o preço do bico baixa só para este cliente) × "Acumular" (cashback e pontos)
  let _desc = null;
  const modoAtual = () => {
    const x = document.querySelector('input[name="a-modo"]:checked');
    return _desc && x ? x.value : "acumular";
  };
  const modo = () => {
    const desc = modoAtual() === "desconto";
    $("a-forma-campo").style.display = desc ? "none" : "";
    $("a-forma-desc-campo").style.display = desc ? "" : "none";
    $("a-btn").textContent = desc ? "Aplicar o desconto na bomba" : "Avisar o caixa";
    $("a-rodape").innerHTML = desc
      ? "O preço baixa <b>só neste bico e só para você</b> e volta ao normal quando terminar. Com o desconto, este abastecimento não gera cashback."
      : "O caixa recebe o seu nome na hora. A compra vale <b>1 ponto a cada R$ 1</b>.";
  };
  const descontoBico = async () => {
    const b = soDigitos($("a-bico").value), posto = $("a-posto").value;
    _desc = null; $("a-modo").innerHTML = ""; modo();
    if (!b || !posto) return;
    const r = await api(`/cashback/api/desconto?posto=${encodeURIComponent(posto)}&bico=${b}`);
    if (!(r.ok && r.ativo) || soDigitos($("a-bico").value) !== b) return;
    _desc = r;
    $("a-modo").innerHTML = `<div class="campo"><label>O que você prefere?</label>
      <label class="check" style="margin:6px 0;align-items:flex-start"><input type="radio" name="a-modo" value="desconto" checked>
        <span><b>Desconto agora</b> · ${brl(r.desconto_litro)} por litro: o bico fica em <b>${brl(r.preco_desconto)}</b> em vez de ${brl(r.preco_normal)}</span></label>
      <label class="check" style="margin:6px 0;align-items:flex-start"><input type="radio" name="a-modo" value="acumular">
        <span><b>Acumular</b> · cashback e pontos para resgatar depois</span></label></div>`;
    $("a-forma-desc").innerHTML = r.formas.map(f => `<option value="${esc(f.id)}">${esc(f.nome)}</option>`).join("");
    document.querySelectorAll('input[name="a-modo"]').forEach(x => { x.onchange = modo; });
    modo();
  };
  const infoBico = async () => {
    const b = soDigitos($("a-bico").value), posto = $("a-posto").value;
    if (!b || !posto) { $("a-info").textContent = ""; descontoBico(); return; }
    const r = await api(`/cashback/api/bico?posto=${encodeURIComponent(posto)}&bico=${b}`);
    if (r.ok && r.combustivel) {
      $("a-comb").value = r.combustivel;
      $("a-info").innerHTML = `⛽ <b>${esc(r.combustivel)}</b>${r.preco_litro ? " · " + brl(r.preco_litro) + "/L" : ""}`;
    } else $("a-info").textContent = "";
    descontoBico();
  };
  const prazo = async () => {
    const posto = $("a-posto").value, sel = $("a-forma");
    const op = sel.querySelector('option[value="05"]');
    if (!posto) return;
    const r = await api("/cashback/api/prazo-status?posto=" + encodeURIComponent(posto));
    if (r.prazo && !op) sel.insertAdjacentHTML("beforeend", `<option value="05">A prazo${r.empresa ? " (conta " + esc(r.empresa) + ")" : ""}</option>`);
    if (!r.prazo && op) op.remove();
    $("a-prazo-msg").textContent = r.prazo ? "" : (r.motivo ? "A prazo: " + r.motivo : "");
  };
  $("a-posto").onchange = () => { S.posto = $("a-posto").value; LS.set("psn_posto", S.posto); infoBico(); prazo(); };
  let t = null;
  $("a-bico").addEventListener("input", () => { clearTimeout(t); t = setTimeout(infoBico, 400); });
  infoBico(); prazo();
  $("a-btn").onclick = async () => {
    const m = $("a-msg"), posto = $("a-posto").value, forma = $("a-forma").value;
    m.className = "msg erro";
    if (!posto) { m.textContent = "Escolha o posto."; return; }
    if (!soDigitos($("a-bico").value)) { m.textContent = "Informe o número do bico."; return; }
    if (modoAtual() === "desconto") {
      m.className = "msg"; m.textContent = "Pedindo o desconto à bomba…";
      const rd = await api("/cashback/api/acionar", { posto, bico: $("a-bico").value, combustivel: $("a-comb").value,
                                                      modo: "desconto", forma_id: $("a-forma-desc").value });
      if (!rd.ok) { m.className = "msg erro"; m.textContent = rd.erro || "Não consegui pedir o desconto."; return; }
      S.bico = "";
      acompanhar();
      return;
    }
    let selfie = null;
    if (forma === "05") {
      if (!(S.me && S.me.tem_facial)) {
        const termo = await api("/cashback/api/termo");
        const ok = await fotoRosto("Foto do seu rosto para o cadastro (uma vez só)", termo.texto || "");
        if (!ok) { m.textContent = "Sem a foto, a compra a prazo é feita no caixa."; return; }
        const r1 = await api("/cashback/api/facial", { foto: ok, aceite: true });
        if (!r1.ok) { m.textContent = r1.erro || "Não consegui salvar a foto."; return; }
        if (S.me) S.me.tem_facial = true;
      }
      selfie = await fotoRosto("Sua foto autoriza esta compra a prazo", "");
      if (!selfie) { m.textContent = "Sem a foto, a compra a prazo não é autorizada pelo app."; return; }
    }
    m.className = "msg"; m.textContent = "Avisando o caixa…";
    const r = await api("/cashback/api/acionar", { posto, bico: $("a-bico").value, combustivel: $("a-comb").value, forma, selfie });
    if (!r.ok) { m.className = "msg erro"; m.textContent = r.erro || "Não consegui avisar o caixa."; return; }
    S.bico = "";
    if (r.aviso) toast(r.aviso, 4500);
    acompanhar();
  };
}

// ---------------------------------------------------------------- ao vivo
function acompanhar() {
  el().innerHTML = `<div class="tela">${topoSimples("Seu abastecimento", false)}
    <div class="secao"><div class="card vivo">
      <div id="v-fase" style="color:var(--mut);font-weight:600">Avisando o caixa…</div>
      <div class="num" id="v-num">—</div><div id="v-det" style="color:var(--mut)"></div>
      <div id="v-acoes"></div>
      <button class="btn sec" id="v-cancelar">Cancelar</button>
      <button class="btn" onclick="ir('#/inicio')">Voltar ao início</button>
    </div></div></div>`;
  document.getElementById("v-cancelar").onclick = async () => {
    await api("/cashback/api/acionar/cancelar", {});
    pararVivo(); toast("Cancelado."); ir("#/inicio");
  };
  const passo = async () => {
    const r = await api("/cashback/api/acionamento/live");
    const fase = document.getElementById("v-fase"), n = document.getElementById("v-num"), d = document.getElementById("v-det");
    if (!fase) { pararVivo(); return; }
    const ac = r.acionamento || {};
    const canc = document.getElementById("v-cancelar");      // depois de abastecer não há mais o que cancelar
    if (canc) canc.style.display = ["concluido", "cashback", "usado"].includes(r.fase) ? "none" : "";
    if (!r.ok || r.fase === "sem_acionamento") { fase.textContent = "Nenhum abastecimento acionado."; pararVivo(); return; }
    // DESCONTO DO APP NA BOMBA: enquanto a bomba troca o preço, ainda não pode abastecer
    const desc = ac.modo === "desconto";
    const acoes = document.getElementById("v-acoes");
    const deNovo = rotulo => {
      if (!acoes || document.getElementById("v-denovo")) return;
      acoes.innerHTML = `<button class="btn" id="v-denovo">${esc(rotulo)}</button>`;
      document.getElementById("v-denovo").onclick = () => {
        pararVivo();
        if (ac.empresa_id) { S.posto = ac.empresa_id; LS.set("psn_posto", S.posto); }
        S.bico = String(ac.bico || "");
        ir("#/acionar");
      };
    };
    if (acoes && !String(r.fase || "").startsWith("desconto_")) acoes.innerHTML = "";
    if (r.fase === "desconto_aplicando") {
      fase.textContent = "⏳ Aplicando o desconto no bico " + (ac.bico || "?"); n.textContent = brl(ac.preco_desconto) + "/L";
      d.textContent = "Aguarde uns segundos — ainda não tire o bico.";
    } else if (r.fase === "desconto_sem_resposta") {
      fase.textContent = "⚠️ O posto não respondeu"; n.textContent = "—";
      d.textContent = "Não consegui trocar o preço da bomba agora. Você pode escolher de novo e abastecer acumulando cashback e pontos.";
      deNovo("Escolher de novo");
    } else if (["desconto_falhou", "desconto_expirado", "desconto_cancelado"].includes(r.fase)) {
      fase.textContent = r.fase === "desconto_expirado" ? "⌛ O desconto expirou" : r.fase === "desconto_cancelado" ? "Desconto cancelado" : "⚠️ Não deu para aplicar o desconto";
      n.textContent = "—";
      d.textContent = r.fase === "desconto_expirado"
        ? "Você não começou a abastecer em 3 minutos e o preço do bico voltou ao normal."
        : (r.erro ? "Motivo: " + r.erro + ". " : "") + "O preço do bico continua o normal.";
      if (r.fase !== "desconto_cancelado") deNovo(r.fase === "desconto_expirado" ? "Pedir de novo" : "Escolher de novo");
      pararVivo();
    } else if (r.fase === "aguardando_inicio" && desc) {
      fase.textContent = "✅ Desconto na bomba! Pode abastecer no bico " + (ac.bico || "?");
      n.textContent = brl(ac.preco_desconto) + "/L";
      d.textContent = "Preço normal " + brl(ac.preco_normal) + "/L. Se não começar em 3 minutos, o preço volta ao normal.";
    } else if (r.fase === "aguardando_inicio") { fase.textContent = "⏳ Pode abastecer no bico " + (ac.bico || "?"); n.textContent = "—"; d.textContent = "O caixa já sabe que é você."; }
    else if (r.fase === "abastecendo") {
      const lv = r.live || {};
      fase.textContent = "⛽ Abastecendo no bico " + (ac.bico || "?") + (desc ? " com desconto" : "");
      n.textContent = lv.volume != null ? Number(lv.volume).toLocaleString("pt-BR", { minimumFractionDigits: 2 }) + " L" : brl(lv.valor);
      d.textContent = (lv.valor != null ? brl(lv.valor) + " · " : "") + (lv.combustivel || "");
    } else if (r.fase === "concluido") {
      const a = r.abastecimento || {};
      const litros = Number(a.litros || 0);
      fase.textContent = "✅ Abastecimento concluído";
      n.textContent = brl(a.valor || a.valor_total);
      d.textContent = litros.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " L · pague no caixa. "
        + (desc ? "Você economizou " + brl(litros * Number(ac.desconto_litro || 0)) + "." : "Você ganha " + num(Math.floor(a.valor || a.valor_total || 0)) + " pontos.");
    } else if (r.fase === "cashback") {
      const c = r.cashback || {};
      fase.textContent = c.status === "pago" ? "🎉 Cashback pago no seu Pix" : "🕐 Cashback a caminho";
      n.textContent = brl(c.valor_cashback);
      d.textContent = "Obrigado pela preferência!";
      if (c.status === "pago") pararVivo();
    } else if (r.fase === "usado") {
      fase.textContent = "🧾 Compra registrada"; n.textContent = ac.venda_numero ? "cupom " + ac.venda_numero : "✓";
      d.textContent = "Os pontos entram em alguns minutos. Bom trajeto!";
      pararVivo();
    } else { fase.textContent = "Acionamento " + r.fase; pararVivo(); }
  };
  pararVivo();
  _vivoTimer = setInterval(passo, 2500);
  passo();
}

// ---------------------------------------------------------------- foto do rosto (a prazo)
function fotoRosto(titulo, termo) {
  return new Promise(async resolve => {
    pararCamera();
    const c = document.createElement("div");
    c.className = "camera"; c.id = "camera";
    c.innerHTML = `<button class="fechar" id="f-x" aria-label="Cancelar">${ICO.x(22)}</button>
      <div style="font-weight:700;margin-bottom:12px;text-align:center">${esc(titulo)}</div>
      ${termo ? `<div style="max-width:420px;max-height:150px;overflow:auto;background:rgba(255,255,255,.08);border-radius:12px;padding:10px;font-size:.74rem;white-space:pre-wrap;margin-bottom:10px">${esc(termo)}</div>
        <label class="check" style="color:#fff;max-width:420px"><input type="checkbox" id="f-aceite"> <span>Li e aceito o termo de uso da minha imagem.</span></label>` : ""}
      <video id="f-video" class="rosto" playsinline muted autoplay style="margin-top:12px"></video>
      <div id="f-msg" style="margin-top:10px;font-size:.85rem;opacity:.85">Abrindo a câmera…</div>
      <button class="btn" id="f-tirar" style="max-width:420px">${ICO.camera(18)} &nbsp;Tirar a foto</button>`;
    document.body.appendChild(c);
    const fim = v => { pararCamera(); resolve(v); };
    document.getElementById("f-x").onclick = () => fim(null);
    try {
      _cam = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 640 } }, audio: false });
      const v = document.getElementById("f-video");
      v.srcObject = _cam;
      try { await v.play(); } catch (e) { /* toca sozinho */ }
      document.getElementById("f-msg").textContent = "Enquadre o rosto no círculo.";
    } catch (e) { document.getElementById("f-msg").textContent = "A câmera não abriu. Sem a foto, faça a compra a prazo no caixa."; }
    document.getElementById("f-tirar").onclick = () => {
      const ac = document.getElementById("f-aceite");
      if (ac && !ac.checked) { document.getElementById("f-msg").textContent = "Marque que leu e aceita o termo."; return; }
      const v = document.getElementById("f-video");
      if (!_cam || !v.videoWidth) return;
      const esc2 = Math.min(1, 640 / Math.max(v.videoWidth, v.videoHeight));
      const cv = document.createElement("canvas");
      cv.width = Math.round(v.videoWidth * esc2); cv.height = Math.round(v.videoHeight * esc2);
      cv.getContext("2d").drawImage(v, 0, 0, cv.width, cv.height);
      fim(cv.toDataURL("image/jpeg", 0.82));
    };
  });
}
