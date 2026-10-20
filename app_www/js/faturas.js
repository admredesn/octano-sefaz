// ============================================================
// App Postos SN — ÁREA DO CLIENTE: faturas (13/10/2026)
// ------------------------------------------------------------
// Faturas em aberto e pagas, extrato detalhado (cupom, produto, placa), boleto (linha digitável + PDF),
// fatura em PDF e FOTO DO CUPOM ASSINADO. Funciona igual no celular (app) e no PC (https://…/app/).
// Servidor: cliente_faturas.py (rotas /cashback/api/faturas/*). Tudo que vem do servidor passa por esc().
// Quem vê: o próprio cadastro; a empresa só para colaborador marcado no retaguarda.
// ============================================================

COM_BARRA.push("faturas", "fatura");

// ---------------------------------------------------------------- utilidades
function fatData(s) {                 // "2026-10-06" (só data: sem fuso) ou ISO completo
  if (!s) return "";
  const t = String(s);
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(t);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : dataBr(t);
}
function fatQtd(q) { return Number(q || 0).toLocaleString("pt-BR", { maximumFractionDigits: 3 }); }

// aviso de credito bloqueado (texto aprovado pelo Ronan em 14/10/2026)
function fatAvisoBloqueio(p) {
  if (!p || !p.bloqueado) return "";
  const t = Number(p.vencidas_valor || 0) > 0
    ? `Seu crédito a prazo está bloqueado: você tem <b>${brl(p.vencidas_valor)}</b> em fatura vencida. Regularize para voltar a comprar a prazo.`
    : "Seu crédito a prazo está bloqueado. Fale com o posto.";
  return `<div class="alerta-bloq" role="alert"><span class="ic">${ICO.alerta(22)}</span><div>${t}</div></div>`;
}

function fatTag(f) {
  if (f.status === "liquidada") return `<span class="tag verde">Paga${f.liquidado_em ? " em " + esc(fatData(f.liquidado_em)) : ""}</span>`;
  if (f.vencida) return `<span class="tag verm">Vencida${f.dias_atraso ? " há " + f.dias_atraso + " dia" + (f.dias_atraso > 1 ? "s" : "") : ""}</span>`;
  return `<span class="tag ouro">Vence em ${esc(fatData(f.vencimento))}</span>`;
}

// ---------------------------------------------------------------- resumo (Início e Perfil)
// Reaproveita o ultimo resumo por 30 s (o Inicio acabou de buscar; clicar em "Faturas" nao busca de novo).
async function faturasResumo(forcar) {
  if (S.fat && !forcar && Date.now() - (S.fatT || 0) < 30000) return S.fat;
  const r = await api("/cashback/api/faturas/resumo");
  S.fat = r && r.ok ? r : { ok: true, ativo: false, postos: [] };
  S.fatT = Date.now();
  return S.fat;
}

// ---- cache curto: a tela aparece na hora com o que ja' temos e e' atualizada em seguida ----
function fatEsqueleto(cab) {
  return `<div class="tela">${cab}<div class="secao" style="padding-top:2px">
    <div class="fat-resumo"><div class="sk" style="height:76px"></div><div class="sk" style="height:76px"></div></div>
    <div class="sk" style="height:18px;width:46%;margin:18px 0 10px"></div>
    <div class="sk" style="height:64px;margin-bottom:8px"></div><div class="sk" style="height:64px"></div></div></div>`;
}
function fatAindaAqui(prefixo) { return (location.hash || "").indexOf(prefixo) === 0; }
function fatLimparCache() { S.fat = null; S.fatT = 0; S.fatLista = {}; S.fatDet = {}; }

// pre-busca (Inicio): a lista do posto unico e a fatura mais urgente ja' ficam na memoria
function fatPrefetch(posto) {
  S.fatLista = S.fatLista || {}; S.fatDet = S.fatDet || {};
  const c = S.fatLista[posto];
  if (c && Date.now() - c.t < 30000) return;
  api("/cashback/api/faturas?posto=" + encodeURIComponent(posto)).then(r => {
    if (!r || !r.ok) return;
    S.fatLista[posto] = { t: Date.now(), r };
    const urgente = (r.abertas || [])[0];
    if (urgente && !S.fatDet[urgente.id]) {
      api("/cashback/api/faturas/" + encodeURIComponent(urgente.id)).then(d => {
        if (d && d.ok) S.fatDet[urgente.id] = { t: Date.now(), r: d };
      }).catch(() => {});
    }
  }).catch(() => {});
}

async function faturasPreencher() {
  try {
    const r = await faturasResumo(true);
    if (!r.ativo || !r.postos.length) return;
    const aberta = r.postos.reduce((s, p) => s + Number(p.abertas_valor || 0), 0);
    const qtd = r.postos.reduce((s, p) => s + Number(p.abertas_qtd || 0), 0);
    const venc = r.postos.reduce((s, p) => s + Number(p.vencidas_qtd || 0), 0);
    const nao = r.postos.reduce((s, p) => s + Number(p.nao_faturado_valor || 0), 0);
    const alvo = r.postos.length === 1 ? "#/faturas/" + r.postos[0].empresa_id : "#/faturas";
    if (r.postos.length === 1) fatPrefetch(r.postos[0].empresa_id);
    const card = document.getElementById("fat-card");
    const avisos = r.postos.filter(p => p.bloqueado).map(fatAvisoBloqueio).join("");
    if (card) {
      card.innerHTML = (avisos ? `<div class="secao" style="padding-bottom:0">${avisos}</div>` : "") + `<div class="secao"><button class="card fat-card ${venc ? "venc" : ""}" onclick="ir('${alvo}')">
        <span class="ic">${ICO.prazo(22)}</span>
        <div class="meio"><div class="t">${qtd ? "Faturas em aberto" : "Suas faturas"}</div>
          <div class="s">${qtd ? `${qtd} fatura${qtd > 1 ? "s" : ""} · ${brl(aberta)}${venc ? ` · <b style="color:var(--verm)">${venc} vencida${venc > 1 ? "s" : ""}</b>` : ""}`
            : (nao > 0 ? `Compras a prazo ainda não faturadas: ${brl(nao)}` : "Nenhuma fatura em aberto")}</div></div>
        <span class="seta">${ICO.seta(18)}</span></button></div>`;
    }
    const per = document.getElementById("fat-perfil");
    if (per) {
      per.innerHTML = `<div class="grupo-tit">Compras a prazo</div>
        <div class="secao" style="padding-top:0"><div class="card lista">
          <button class="linha" onclick="ir('${alvo}')"><span class="ic">${ICO.prazo(20)}</span>
            <div class="meio"><div class="t">Minhas faturas</div><div class="s">Extrato, boleto e comprovantes dos cupons</div></div>
            <span class="seta">${ICO.seta(18)}</span></button></div></div>`;
    }
  } catch (e) { /* a área de faturas é opcional: se falhar, o resto do app segue */ }
}

// ---------------------------------------------------------------- lista
async function telaFaturas(posto) {
  const cab = topoSimples("Minhas faturas", true);
  S.fatLista = S.fatLista || {};
  // 1) o que ja' esta na memoria aparece NA HORA (senao, o esqueleto)
  const res0 = S.fat && S.fat.ativo ? S.fat : null;
  let alvo = posto || (res0 && res0.postos.length === 1 ? res0.postos[0].empresa_id : "");
  const c0 = alvo && S.fatLista[alvo];
  if (res0 && c0) fatDesenharLista(alvo, res0, c0.r, cab);
  else el().innerHTML = fatEsqueleto(cab);
  // 2) busca o dado novo: lista e resumo ao mesmo tempo
  const fresco = c0 && Date.now() - c0.t < 30000;
  let lstP = alvo && !fresco ? api("/cashback/api/faturas?posto=" + encodeURIComponent(alvo)) : null;
  const res = await faturasResumo(false);
  if (!fatAindaAqui("#/faturas")) return;
  if (!res.ativo || !res.postos.length) {
    el().innerHTML = `<div class="tela">${cab}<div class="secao"><div class="card vazio" style="padding:16px">
      Nenhuma fatura encontrada para o seu cadastro. Se você compra a prazo e deveria ver as faturas aqui, fale com o posto.</div></div></div>`;
    return;
  }
  if (!posto && res.postos.length === 1) posto = res.postos[0].empresa_id;
  if (!posto) {
    el().innerHTML = `<div class="tela">${cab}<div class="secao"><div class="card lista">${res.postos.map(p => `
      <button class="linha" onclick="ir('#/faturas/${esc(p.empresa_id)}')"><span class="ic">${ICO.loja(20)}</span>
        <div class="meio"><div class="t">${esc(p.posto || "Posto")}</div><div class="s">${p.abertas_qtd} em aberto · ${brl(p.abertas_valor)}</div></div>
        <span class="seta">${ICO.seta(18)}</span></button>`).join("")}</div></div></div>`;
    return;
  }
  let r;
  if (fresco && posto === alvo) r = c0.r;
  else {
    if (!lstP || posto !== alvo) lstP = api("/cashback/api/faturas?posto=" + encodeURIComponent(posto));
    r = await lstP;
  }
  if (!fatAindaAqui("#/faturas")) return;
  if (!r.ok) {
    el().innerHTML = `<div class="tela">${cab}<div class="secao"><div class="card vazio" style="padding:16px">${esc(r.erro || "Não consegui carregar as faturas.")}</div></div></div>`;
    return;
  }
  S.fatLista[posto] = { t: Date.now(), r };
  fatDesenharLista(posto, res, r, cab);
}

function fatDesenharLista(posto, res, r, cab) {
  const info = res.postos.find(p => p.empresa_id === posto) || {};
  S.fatPosto = info;
  const total = r.abertas.reduce((s, f) => s + f.valor, 0);
  const linha = f => `<button class="linha" onclick="ir('#/fatura/${esc(f.id)}')">
      <span class="ic">${ICO.cupom(20)}</span>
      <div class="meio"><div class="t">Fatura ${esc(f.numero)}${f.parcela ? " · parcela " + esc(f.parcela) : ""}</div>
        <div class="s">${fatTag(f)}${f.da_empresa ? ` · ${esc(f.titular)}` : ""}</div></div>
      <div class="dir">${brl(f.valor)}</div></button>`;
  const nao = r.nao_faturadas;
  el().innerHTML = `<div class="tela">${cab}
    <div class="secao" style="padding-top:2px"><div class="s" style="color:var(--mut);font-size:.84rem">${esc(info.posto || "")}${info.titular && info.titular.length ? " · " + esc(info.titular.join(", ")) : ""}</div></div>
    ${info.bloqueado ? `<div class="secao" style="padding-bottom:0">${fatAvisoBloqueio(info)}</div>` : ""}
    <div class="secao"><div class="fat-resumo">
      <div class="card"><div class="rot">Em aberto</div><div class="val">${brl(total)}</div><div class="s">${r.abertas.length} fatura${r.abertas.length === 1 ? "" : "s"}</div></div>
      <div class="card"><div class="rot">Ainda sem fatura</div><div class="val">${brl(nao.valor)}</div><div class="s">${nao.qtd} compra${nao.qtd === 1 ? "" : "s"} a prazo</div></div>
    </div></div>
    <div class="secao"><div class="secao-tit"><h2>Faturas em aberto</h2></div>
      ${r.abertas.length ? `<div class="card lista">${r.abertas.map(linha).join("")}</div>`
        : '<div class="card vazio" style="padding:16px">Nenhuma fatura em aberto. 👍</div>'}</div>
    ${nao.qtd ? `<div class="secao"><div class="secao-tit"><h2>Compras ainda não faturadas</h2></div>
      <div class="vazio" style="padding:0 2px 8px">Entram na próxima fatura do posto.</div>
      <div class="card lista">${nao.itens.map(fatLinhaCupom).join("")}</div></div>` : ""}
    ${r.pagas.length ? `<div class="secao"><div class="secao-tit"><h2>Pagas (últimos 12 meses)</h2></div>
      <div class="card lista">${r.pagas.map(linha).join("")}</div></div>` : ""}
  </div>`;
}

function fatLinhaCupom(c) {
  return `<div class="linha"><span class="ic">${ICO.bomba(20)}</span>
    <div class="meio"><div class="t">Cupom ${esc(c.cupom || "")}</div><div class="s">${esc(fatData(c.data))} ${esc(horaBr(c.data))}${c.placa ? " · " + esc(c.placa) : ""}</div></div>
    <div class="dir">${brl(c.valor)}${c.tem_foto ? `<button class="foto-btn" onclick="faturaFoto('${esc(c.nota_id)}')" aria-label="Ver comprovante assinado">${ICO.camera(18)}</button>` : ""}${(S.fatPosto && S.fatPosto.pagamento_app) ? `<button class="pagar-btn" onclick="pagarNota('${esc(c.nota_id)}', ${Number(c.valor || 0)}, '${esc(c.cupom || "")}')">Pagar</button>` : ""}</div></div>`;
}

// ---------------------------------------------------------------- detalhe
async function telaFatura(id) {
  const cab = topoSimples("Fatura", true);
  S.fatDet = S.fatDet || {};
  const c0 = S.fatDet[id];
  if (c0) fatDesenharFatura(c0.r, cab);                       // o que ja' temos, na hora
  else el().innerHTML = fatEsqueleto(cab);
  if (c0 && Date.now() - c0.t < 20000) return;                 // buscada ha' instantes (pre-busca do Inicio)
  const r = await api("/cashback/api/faturas/" + encodeURIComponent(id));
  if (!fatAindaAqui("#/fatura/")) return;
  if (!r.ok) {
    el().innerHTML = `<div class="tela">${cab}<div class="secao"><div class="card vazio" style="padding:16px">${esc(r.erro || "Não consegui abrir esta fatura.")}</div></div></div>`;
    return;
  }
  S.fatDet[id] = { t: Date.now(), r };
  fatDesenharFatura(r, cab);
}

function fatDesenharFatura(r, cab) {
  const f = r.fatura, b = r.boleto;
  // agrupa o extrato por cupom
  const grupos = [];
  const mapa = {};
  r.linhas.forEach(l => {
    const k = String(l.cupom || "") + "|" + String(l.data || "");
    if (!mapa[k]) { mapa[k] = { cupom: l.cupom, data: l.data, placa: l.placa, odometro: l.odometro, veiculo: l.veiculo, itens: [] }; grupos.push(mapa[k]); }
    mapa[k].itens.push(l);
  });
  const foto = {};
  r.cupons.forEach(c => { if (c.tem_foto) foto[String(c.cupom)] = c.nota_id; });
  const totalExtrato = r.linhas.reduce((s, l) => s + Number(l.total || 0), 0);
  el().innerHTML = `<div class="tela">${cab}
    <div class="secao" style="padding-top:2px"><div class="card fat-topo">
      <div class="rot">Fatura ${esc(f.numero)}${f.parcela ? " · parcela " + esc(f.parcela) : ""}</div>
      <div class="val">${brl(f.valor)}</div>
      <div style="margin-top:6px">${fatTag(Object.assign({}, f, { dias_atraso: 0 }))}</div>
      <div class="s" style="margin-top:8px">Emissão ${esc(fatData(f.emissao))} · Vencimento ${esc(fatData(f.vencimento))}${f.da_empresa ? " · " + esc(f.titular) : ""}</div>
      ${f.acrescimo || f.desconto ? `<div class="s">Valor das compras ${brl(f.valor_bruto)}${f.acrescimo ? " + acréscimo " + brl(f.acrescimo) : ""}${f.desconto ? " − desconto " + brl(f.desconto) : ""}</div>` : ""}
    </div></div>
    ${r.pagamento_app && f.status === "aberta" ? `<div class="secao"><button class="btn" onclick="pagarFatura('${esc(f.id)}')">Pagar a fatura</button></div>` : ""}
    ${b && f.status === "aberta" ? fatBoleto(f, b) : ""}
    <div class="secao"><div class="secao-tit"><h2>Documentos</h2></div><div class="card lista">
      <button class="linha" onclick="faturaPdf('${esc(f.id)}','fatura')"><span class="ic">${ICO.dados(20)}</span><div class="meio"><div class="t">Fatura detalhada (PDF)</div></div><span class="seta">${ICO.seta(18)}</span></button>
      ${r.docs.boleto ? `<button class="linha" onclick="faturaPdf('${esc(f.id)}','boleto')"><span class="ic">${ICO.prazo(20)}</span><div class="meio"><div class="t">Boleto (PDF)</div></div><span class="seta">${ICO.seta(18)}</span></button>` : ""}
      ${r.docs.nfe ? `<button class="linha" onclick="faturaPdf('${esc(f.id)}','nfe')"><span class="ic">${ICO.cupom(20)}</span><div class="meio"><div class="t">Nota fiscal (PDF)</div></div><span class="seta">${ICO.seta(18)}</span></button>` : ""}
    </div></div>
    <div class="secao"><div class="secao-tit"><h2>Compras desta fatura</h2><span class="s" style="color:var(--mut)">${grupos.length} ${grupos.length === 1 ? "cupom" : "cupons"}</span></div>
      ${grupos.length ? `<div class="card lista">${grupos.map(g => `<div class="linha cupom-g">
        <div class="meio"><div class="t">${esc(fatData(g.data))} ${esc(horaBr(g.data))} · Cupom ${esc(g.cupom || "")}</div>
          <div class="s">${[g.placa ? "Placa " + esc(g.placa) : "", g.veiculo ? esc(g.veiculo) : "", g.odometro ? "Km " + esc(g.odometro) : ""].filter(Boolean).join(" · ")}</div>
          ${g.itens.map(i => `<div class="item-l"><span>${i.qtd ? fatQtd(i.qtd) + (i.litros ? " L " : " × ") : ""}${esc(String(i.produto || "").toLowerCase())}</span><b>${brl(i.total)}</b></div>`).join("")}
        </div>
        ${foto[String(g.cupom)] ? `<button class="foto-btn" onclick="faturaFoto('${esc(foto[String(g.cupom)])}')" aria-label="Ver comprovante assinado">${ICO.camera(20)}<span>Comprovante</span></button>` : ""}
      </div>`).join("")}</div>
      <div class="vazio" style="text-align:right;padding-right:4px">Soma das compras: <b>${brl(totalExtrato)}</b></div>`
        : '<div class="card vazio" style="padding:16px">O detalhamento desta fatura ainda não está disponível.</div>'}
    </div>
  </div>`;
  if (b && b.pix && f.status === "aberta") fatPixCarregar(f.id);
}

function fatBoleto(f, b) {
  const linhaDig = String(b.linha_digitavel || "");
  return `<div class="secao"><div class="secao-tit"><h2>Pagar</h2></div><div class="card" style="padding:14px 16px">
    ${b.pix ? `<div class="fat-pix"><div id="fat-pix-img" class="pix-img">…</div>
      <div><div style="font-weight:700">Pix</div><div class="s" style="color:var(--mut);font-size:.84rem">Abra o app do seu banco e leia o QR Code, ou copie o código.</div>
      <button class="btn-borda" style="margin-top:8px" data-copiar="${esc(b.pix_copia_cola || "")}" data-aviso="Código Pix copiado" onclick="copiarBtn(this)">Copiar Pix copia e cola</button></div></div><hr class="fat-hr">` : ""}
    ${linhaDig ? `<div style="font-weight:700">Boleto</div>
      <div class="linha-dig">${esc(linhaDig)}</div>
      <button class="btn-borda" data-copiar="${esc(linhaDig)}" data-aviso="Linha digitável copiada" onclick="copiarBtn(this)">Copiar linha digitável</button>`
      : '<div class="s" style="color:var(--mut)">O boleto desta fatura ainda está sendo preparado. Fale com o posto se precisar agora.</div>'}
  </div></div>`;
}

async function fatPixCarregar(id) {
  try {
    const r = await fetch(API + "/cashback/api/faturas/" + encodeURIComponent(id) + "/pix.png", { headers: { Authorization: "Bearer " + tok() } });
    const alvo = document.getElementById("fat-pix-img");
    if (!r.ok || !alvo) return;
    alvo.innerHTML = `<img alt="QR Code Pix" src="${URL.createObjectURL(await r.blob())}">`;
  } catch (e) { /* sem QR: o botão de copiar continua valendo */ }
}

// O texto vai num data-atributo (esc): colocar o codigo dentro do onclick="..." quebrava as aspas e o botao nao fazia nada.
function copiarBtn(b) {
  const orig = b.getAttribute("data-rot") || b.textContent;
  b.setAttribute("data-rot", orig);
  copiarTexto(b.getAttribute("data-copiar") || "", b.getAttribute("data-aviso") || "Copiado", () => {
    b.textContent = "✓ Copiado";
    b.classList.add("copiado");
    setTimeout(() => { b.textContent = orig; b.classList.remove("copiado"); }, 2200);
  });
}

function copiarTexto(txt, aviso, depois) {
  const ok = () => { toast(aviso || "Copiado", 2600); if (depois) depois(); };
  const falha = () => toast("Não consegui copiar. Toque e segure o código para copiar.", 4000);
  // 1) API moderna (precisa ser chamada dentro do toque do usuario)
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(txt).then(ok, () => { copiaAntiga(txt) ? ok() : falha(); });
      return;
    }
  } catch (e) { /* cai no metodo antigo */ }
  copiaAntiga(txt) ? ok() : falha();
}
// iPhone/Safari: o textarea precisa estar editavel e com a selecao feita por faixa
function copiaAntiga(txt) {
  const t = document.createElement("textarea");
  t.value = txt;
  t.setAttribute("readonly", "");
  t.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;font-size:16px";
  document.body.appendChild(t);
  let feito = false;
  try {
    const sel = document.getSelection();
    const salva = sel && sel.rangeCount > 0 ? sel.getRangeAt(0) : null;
    t.focus(); t.select(); t.setSelectionRange(0, txt.length);
    feito = document.execCommand("copy");
    if (salva && sel) { sel.removeAllRanges(); sel.addRange(salva); }
  } catch (e) { feito = false; }
  t.remove();
  return feito;
}

// ---------------------------------------------------------------- PDF e foto (link temporário)
async function faturaPdf(id, tipo) {
  try {
    toast("Abrindo o documento…", 1500);
    for (let t = 0; t < 8; t++) {
      const r = await api("/cashback/api/faturas/" + encodeURIComponent(id) + "/arquivo?tipo=" + encodeURIComponent(tipo));
      if (r.ok && r.url) {
        folha(`<h2 style="font-size:1.15rem">Documento pronto</h2>
          <p style="color:var(--mut);margin:8px 0 14px;font-size:.9rem">O link vale por 5 minutos. Se precisar de novo, toque outra vez.</p>
          <a class="btn" style="display:block;text-align:center;text-decoration:none" href="${esc(r.url)}" target="_blank" rel="noopener">Abrir PDF</a>
          <button class="btn sec" style="margin-top:8px" onclick="fecharFolha()">Fechar</button>`);
        return;
      }
      if (r.ok && r.gerando) { await new Promise(res => setTimeout(res, 3000)); continue; }
      toast(r.erro || "Não consegui abrir o documento.");
      return;
    }
    toast("O documento ainda está sendo gerado. Tente de novo em instantes.");
  } catch (e) { toast("Não consegui abrir o documento."); }
}

async function faturaFoto(notaId) {
  try {
    const r = await api("/cashback/api/faturas/nota/" + encodeURIComponent(notaId) + "/foto");
    if (!r.ok || !r.url) { toast(r.erro || "A foto deste cupom não está disponível."); return; }
    folha(`<h2 style="font-size:1.15rem">Cupom ${esc(r.cupom || "")} assinado</h2>
      <div class="foto-cupom"><img src="${esc(r.url)}" alt="Foto do cupom assinado"></div>
      <a class="btn sec" style="display:block;text-align:center;text-decoration:none;margin-top:10px" href="${esc(r.url)}" target="_blank" rel="noopener">Abrir em tela cheia</a>
      <button class="btn sec" style="margin-top:8px" onclick="fecharFolha()">Fechar</button>`);
  } catch (e) { toast("Não consegui abrir a foto."); }
}

TELAS.faturas = a => telaFaturas(a);
TELAS.fatura = a => telaFatura(a);

// ---------------------------------------------------------------- pagar pelo app (Pix)
// O servidor so' mostra o botao quando o Pix esta habilitado no posto (pagamento_app). O pedido vira cobranca no
// Sicoob em poucos segundos; esta folha acompanha ate o banco confirmar e a baixa ser automatica.
let _pix = { id: null, timer: null };

function pixFecha() { if (_pix.timer) { clearInterval(_pix.timer); _pix.timer = null; } _pix.id = null; fecharFolha(); }

function pixCorpo(html) {
  folha(`<h2 style="font-size:1.15rem">Pagar com Pix</h2>${html}`);
}

async function pagarAlvo(corpo, rotulo) {
  try {
    pixFecha();
    pixCorpo(`<p style="color:var(--mut);margin:8px 0 14px;font-size:.92rem">Gerando o Pix de ${esc(rotulo)}…</p>`);
    const r = await api("/cashback/api/pagar", corpo);
    if (!r.ok) { pixFecha(); toast(r.erro || "Não consegui gerar o Pix agora."); return; }
    _pix.id = r.id;
    const id = r.id;
    let tentativas = 0;
    const passo = async () => {
      if (_pix.id !== id) return;
      tentativas++;
      const s = await api("/cashback/api/pagar/" + encodeURIComponent(id));
      if (_pix.id !== id) return;
      if (s.status === "ativa") {
        pixCorpo(`<p style="color:var(--mut);margin:6px 0 10px;font-size:.9rem">${esc(rotulo)}: <b>${brl(s.valor)}</b>. Abra o app do seu banco, leia o QR Code ou use o Pix copia e cola. A baixa é automática.</p>
          <div class="fat-pix"><div class="pix-img" id="pix-qr">…</div></div>
          <p class="s" style="color:var(--mut);margin-top:10px;font-size:.84rem" id="pix-estado">Aguardando o pagamento…</p>
          <button class="btn-borda" style="margin-top:8px" data-copiar="${esc(s.copia_cola || "")}" data-aviso="Código Pix copiado" onclick="copiarBtn(this)">Copiar Pix copia e cola</button>
          <button class="btn sec" style="margin-top:8px" onclick="pixFecha()">Fechar</button>`);
        pixQr(id);
        _pix.timer = setInterval(async () => {
          if (_pix.id !== id) return;
          const x = await api("/cashback/api/pagar/" + encodeURIComponent(id));
          if (x.status === "paga") {
            clearInterval(_pix.timer); _pix.timer = null;
            pixCorpo(`<div style="text-align:center;padding:10px 0 4px"><div style="font-size:2.4rem">✓</div>
              <p style="font-weight:800;font-size:1.1rem;margin:6px 0">Pagamento recebido</p>
              <p style="color:var(--mut);font-size:.9rem">${brl(x.valor)} pagos. A baixa já foi feita.</p></div>
              <button class="btn" style="margin-top:12px" onclick="pixFecha(); fatRecarregar()">Concluir</button>`);
          } else if (x.status === "expirada" || x.status === "erro") {
            clearInterval(_pix.timer); _pix.timer = null;
            pixCorpo(`<p style="color:var(--mut);margin:8px 0 14px;font-size:.92rem">${esc(x.erro || "Este Pix não está mais disponível.")}</p>
              <button class="btn sec" onclick="pixFecha()">Fechar</button>`);
          }
        }, 1500);
        return;
      }
      if (s.status === "paga") { pixFecha(); toast("Este Pix já foi pago."); fatRecarregar(); return; }
      if (s.status === "erro" || s.status === "expirada") { pixFecha(); toast(s.erro || "Não consegui gerar o Pix agora."); return; }
      if (tentativas > 45) { pixFecha(); toast("O Pix está demorando para ser gerado. Tente de novo em instantes."); return; }
      setTimeout(passo, 700);
    };
    setTimeout(passo, 500);
  } catch (e) { pixFecha(); toast("Não consegui gerar o Pix agora."); }
}

async function pixQr(id) {
  try {
    const r = await fetch(API + "/cashback/api/pagar/" + encodeURIComponent(id) + "/qr.png", { headers: { Authorization: "Bearer " + tok() } });
    const alvo = document.getElementById("pix-qr");
    if (!r.ok || !alvo) return;
    alvo.innerHTML = `<img alt="QR Code Pix" src="${URL.createObjectURL(await r.blob())}">`;
  } catch (e) { /* sem QR: o botao de copiar continua valendo */ }
}

// 20/10/2026: com o cartão ligado no posto, o Pagar abre a escolha da forma (cartao.js); senão, o Pix de sempre
function pagarNota(notaId, valor, cupom) {
  return pagarEscolher("nota", notaId, "Cupom " + (cupom || ""), valor, S.fatPosto && S.fatPosto.empresa_id);
}
function pagarFatura(faturaId) {
  const f = ((S.fatDet || {})[faturaId] || {}).r ? S.fatDet[faturaId].r.fatura : null;
  return pagarEscolher("fatura", faturaId, f ? "Fatura " + f.numero : "Fatura", f ? f.valor : 0,
                       (f && f.empresa_id) || (S.fatPosto && S.fatPosto.empresa_id));
}

// depois de pagar: recarrega a tela em que o cliente esta (lista ou fatura)
function fatRecarregar() {
  fatLimparCache();
  try { if (typeof rota === "function") rota(); else location.reload(); } catch (e) { location.reload(); }
}
