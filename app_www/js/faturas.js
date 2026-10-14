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
async function faturasResumo(forcar) {
  if (S.fat && !forcar) return S.fat;
  const r = await api("/cashback/api/faturas/resumo");
  S.fat = r && r.ok ? r : { ok: true, ativo: false, postos: [] };
  return S.fat;
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
  const res = await faturasResumo(true);
  const cab = topoSimples("Minhas faturas", true);
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
  const r = await api("/cashback/api/faturas?posto=" + encodeURIComponent(posto));
  if (!r.ok) {
    el().innerHTML = `<div class="tela">${cab}<div class="secao"><div class="card vazio" style="padding:16px">${esc(r.erro || "Não consegui carregar as faturas.")}</div></div></div>`;
    return;
  }
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
    <div class="dir">${brl(c.valor)}${c.tem_foto ? `<button class="foto-btn" onclick="faturaFoto('${esc(c.nota_id)}')" aria-label="Ver comprovante assinado">${ICO.camera(18)}</button>` : ""}${(S.fatPosto && S.fatPosto.pagamento_app) ? `<button class="pagar-btn" onclick="pagarNota('${esc(c.nota_id)}')">Pagar</button>` : ""}</div></div>`;
}

// ---------------------------------------------------------------- detalhe
async function telaFatura(id) {
  const r = await api("/cashback/api/faturas/" + encodeURIComponent(id));
  const cab = topoSimples("Fatura", true);
  if (!r.ok) {
    el().innerHTML = `<div class="tela">${cab}<div class="secao"><div class="card vazio" style="padding:16px">${esc(r.erro || "Não consegui abrir esta fatura.")}</div></div></div>`;
    return;
  }
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
      <button class="btn-borda" style="margin-top:8px" onclick="copiarTexto(${JSON.stringify(String(b.pix_copia_cola || ""))}, 'Código Pix copiado')">Copiar Pix copia e cola</button></div></div><hr class="fat-hr">` : ""}
    ${linhaDig ? `<div style="font-weight:700">Boleto</div>
      <div class="linha-dig">${esc(linhaDig)}</div>
      <button class="btn-borda" onclick="copiarTexto(${JSON.stringify(linhaDig)}, 'Linha digitável copiada')">Copiar linha digitável</button>`
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

function copiarTexto(txt, aviso) {
  const ok = () => toast(aviso || "Copiado");
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(txt).then(ok, () => copiaAntiga(txt, ok)); return; }
  } catch (e) { /* cai no método antigo */ }
  copiaAntiga(txt, ok);
}
function copiaAntiga(txt, ok) {
  const t = document.createElement("textarea");
  t.value = txt; t.style.position = "fixed"; t.style.opacity = "0";
  document.body.appendChild(t); t.select();
  try { document.execCommand("copy"); ok(); } catch (e) { toast("Não consegui copiar. Selecione e copie manualmente."); }
  t.remove();
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

// ---------------------------------------------------------------- pagar UMA compra (Pix) — botao "Pagar"
// O servidor so' mostra o botao quando o Pix esta habilitado no posto (resumo.pagamento_app).
async function pagarNota(notaId) {
  try {
    toast("Gerando o Pix…", 1500);
    const r = await api("/cashback/api/pagar", { nota_id: notaId });
    if (!r.ok) { toast(r.erro || "Não consegui gerar o Pix agora."); return; }
    folha(`<h2 style="font-size:1.15rem">Pagar com Pix</h2>
      <p style="color:var(--mut);margin:6px 0 10px;font-size:.9rem">Valor: <b>${brl(r.valor)}</b>. Abra o app do seu banco, leia o QR Code ou use o Pix copia e cola. A baixa é automática.</p>
      <div class="fat-pix"><div class="pix-img"><img alt="QR Code Pix" src="${esc(r.qr_url || "")}"></div></div>
      <button class="btn-borda" style="margin-top:10px" onclick="copiarTexto(${JSON.stringify(String(r.copia_cola || ""))}, 'Código Pix copiado')">Copiar Pix copia e cola</button>
      <button class="btn sec" style="margin-top:8px" onclick="fecharFolha()">Fechar</button>`);
  } catch (e) { toast("Não consegui gerar o Pix agora."); }
}
