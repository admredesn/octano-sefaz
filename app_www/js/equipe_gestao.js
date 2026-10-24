// ============================================================
// App Postos SN — GESTÃO DA EQUIPE, fase 1 (17/10/2026)
// ------------------------------------------------------------
// Pedido do Ronan: trazer as funções do app gerencial do TecnoX para o Acesso da equipe, com o visual do app
// (fotos de referência em app-postos-sn/tecnox-referencia-2026-10-17). Fase 1:
//  * VENDAS com período e posto: combustível (litros, faturamento, veículos, tickets, média por dia, previsão do
//    mês), por combustível, por horário, loja (itens, valor, ticket, participação) e formas de pagamento;
//  * COMPARAR dois períodos (+ evolução dos últimos meses);
//  * ESTOQUE do sistema × medidor do tanque, com o que ainda cabe;
//  * COMISSÃO dos vendedores: a tela fica pronta com as vendas de cada um; o valor aparece quando a regra existir.
// Rotas /app/equipe/api/vendas | comparativo | estoque | comissao (app_equipe.py). Como no resto da equipe, é o
// SERVIDOR que deixa de mandar o que o perfil não pode ver (R$ sem ver_vendas, litros sem ver_litros…).
// ============================================================

["eq-vendas", "eq-gestao", "eq-comparativo", "eq-estoque", "eq-comissao", "eq-descargas"].forEach(r => { EQ_ROTAS.push(r); PUBLICAS.push(r); });

// período e posto escolhidos: valem para todas as telas da gestão enquanto o app está aberto
const EQV = { per: "hoje", de: "", ate: "", posto: "", postos: [], cmp: "anterior", cde: "", cate: "" };
const EQV_PER = [["hoje", "Hoje"], ["ontem", "Ontem"], ["7d", "7 dias"], ["mes", "Este mês"], ["mes_ant", "Mês passado"], ["custom", "Período"]];

function _dIso(d) { return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); }
function _dBr(iso) { const p = String(iso || "").split("-"); return p.length === 3 ? p[2] + "/" + p[1] : ""; }
function _dMais(iso, dias) { const d = new Date(iso + "T12:00:00"); d.setDate(d.getDate() + dias); return _dIso(d); }
function _dDias(de, ate) { return Math.round((new Date(ate + "T12:00:00") - new Date(de + "T12:00:00")) / 864e5) + 1; }

function eqvPeriodo() {
  const hoje = new Date(), h = _dIso(hoje);
  const ini = new Date(hoje.getFullYear(), hoje.getMonth(), 1);
  switch (EQV.per) {
    case "ontem": { const o = _dMais(h, -1); return { de: o, ate: o }; }
    case "7d": return { de: _dMais(h, -6), ate: h };
    case "mes": return { de: _dIso(ini), ate: h };
    case "mes_ant": return { de: _dIso(new Date(hoje.getFullYear(), hoje.getMonth() - 1, 1)), ate: _dMais(_dIso(ini), -1) };
    case "custom": if (EQV.de && EQV.ate) return { de: EQV.de, ate: EQV.ate }; break;
  }
  return { de: h, ate: h };
}
function eqvRotulo(p) {
  const h = _dIso(new Date());
  if (p.de === p.ate) return p.de === h ? "Hoje, " + _dBr(p.de) : _dBr(p.de);
  return _dBr(p.de) + " a " + _dBr(p.ate) + " · " + _dDias(p.de, p.ate) + " dias";
}
function eqvQuery(p) { return "de=" + p.de + "&ate=" + p.ate + (EQV.posto ? "&posto=" + encodeURIComponent(EQV.posto) : ""); }
function eqvNomePosto() { const x = (EQV.postos || []).find(p => p[0] === EQV.posto); return x ? x[1] : "Todos os postos"; }

// filtro de posto + atalhos de período; `recarregar` = a tela que chamou (abre de novo com o filtro novo)
function eqvFiltros(recarregar, semPeriodo) {
  const p = eqvPeriodo();
  window._eqvRecarregar = recarregar;
  const umPosto = (EQV.postos || []).length === 1;
  return `<div class="filtros">
    ${umPosto ? "" : `<button class="filtro-posto" onclick="eqvEscolherPosto()">${ICO.loja(16)}<b>${esc(eqvNomePosto())}</b>${ICO.seta(14)}</button>`}
    ${semPeriodo ? "" : `<div class="chips">${EQV_PER.map(x => `<button class="chip ${EQV.per === x[0] ? "on" : ""}" onclick="eqvMudar('${x[0]}')">${x[1]}</button>`).join("")}</div>
    <div class="per-txt">${esc(eqvRotulo(p))}</div>`}
  </div>`;
}
function eqvMudar(per) {
  if (per === "custom") { eqvEscolherDatas(); return; }
  EQV.per = per;
  if (window._eqvRecarregar) window._eqvRecarregar();
}
function eqvEscolherPosto() {
  const op = [["", "Todos os postos"]].concat(EQV.postos || []);
  folha(`<h2 style="font-size:1.15rem;margin-bottom:8px">Qual posto?</h2><div class="card lista">${op.map(o => `
    <button class="linha" onclick="EQV.posto='${esc(o[0])}';fecharFolha();if(window._eqvRecarregar)window._eqvRecarregar()">
      <span class="ic">${ICO.loja(20)}</span><div class="meio"><div class="t">${esc(o[1])}</div></div>${EQV.posto === o[0] ? `<span style="color:var(--verde)">${ICO.check(20)}</span>` : ""}</button>`).join("")}</div>`);
}
function eqvEscolherDatas(alvo) {          // alvo = "cmp": o 2º período do comparativo
  const h = _dIso(new Date()), p = eqvPeriodo();
  const de0 = alvo === "cmp" ? (EQV.cde || p.de) : (EQV.de || p.de), ate0 = alvo === "cmp" ? (EQV.cate || p.ate) : (EQV.ate || p.ate);
  folha(`<h2 style="font-size:1.15rem">${alvo === "cmp" ? "Comparar com" : "Escolher o período"}</h2>
    <div class="duas"><div class="campo"><label for="pd-de">De</label><input id="pd-de" type="date" max="${h}" value="${de0}"></div>
      <div class="campo"><label for="pd-ate">Até</label><input id="pd-ate" type="date" max="${h}" value="${ate0}"></div></div>
    <div class="msg" id="pd-msg"></div>
    <button class="btn" id="pd-ok">Ver</button><button class="btn sec" onclick="fecharFolha()">Cancelar</button>`);
  document.getElementById("pd-ok").onclick = () => {
    const de = document.getElementById("pd-de").value, ate = document.getElementById("pd-ate").value;
    const m = document.getElementById("pd-msg");
    if (!de || !ate || de > ate) { m.className = "msg erro"; m.textContent = "A data inicial tem de ser antes da final."; return; }
    if (_dDias(de, ate) > 400) { m.className = "msg erro"; m.textContent = "Escolha até 13 meses."; return; }
    if (alvo === "cmp") { EQV.cmp = "custom"; EQV.cde = de; EQV.cate = ate; } else { EQV.per = "custom"; EQV.de = de; EQV.ate = ate; }
    fecharFolha();
    if (window._eqvRecarregar) window._eqvRecarregar();
  };
}

function eqvCarregando(titulo, voltar, recarregar, semPeriodo) {      // com os filtros na tela: o chip escolhido acende na hora
  el().innerHTML = `<div class="tela">${topoSimples(titulo, voltar)}${recarregar ? eqvFiltros(recarregar, semPeriodo) : ""}<div class="secao" style="padding-top:${recarregar ? 12 : 2}px">
    <div class="sk" style="height:34px;width:60%;margin-bottom:12px"></div><div class="sk" style="height:110px;margin-bottom:12px"></div><div class="sk" style="height:180px"></div></div></div>`;
}
function eqvErro(titulo, voltar, r, recarregar) {
  el().innerHTML = `<div class="tela">${topoSimples(titulo, voltar)}${eqvFiltros(recarregar)}<div class="secao"><div class="card vazio" style="padding:16px">${esc(r.erro || "Não consegui buscar agora. Tente de novo.")}</div></div></div>`;
}
const _n = v => v != null && v !== "" && !isNaN(v);
function _l(v) { return num(Math.round(v || 0)) + " L"; }
function _pct(v) { return (Math.round((v || 0) * 10) / 10).toLocaleString("pt-BR") + "%"; }
function _celula(rot, val, sub) { return `<div class="cel"><div class="rot">${rot}</div><div class="val">${val}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`; }
function _grade(cels) { const c = cels.filter(Boolean); return c.length ? `<div class="card grade">${c.join("")}</div>` : ""; }

// barras horizontais: itens = [[rótulo, valor da barra, texto à direita, texto pequeno]]
function eqvBarras(itens, cores) {
  const max = Math.max(1, ...itens.map(x => Number(x[1]) || 0));
  return `<div class="card barras">${itens.map((x, i) => `<div class="b"><div class="cab"><span>${esc(x[0])}</span><b>${x[2]}${x[3] ? ` <small>${x[3]}</small>` : ""}</b></div>
    <div class="barra-h"><i class="${cores ? "c" + ((i % 4) + 1) : ""}" style="width:${Math.max(1.5, (Number(x[1]) || 0) / max * 100)}%"></i></div></div>`).join("")}</div>`;
}
// colunas: pontos = [[rótulo curto, valor]]. Poucas colunas (meses): número em cima de todas;
// muitas (horas): só a maior ganha cor e número — vizinhas com número embolavam
function eqvColunas(pontos, fmt) {
  const vals = pontos.map(p => Number(p[1]) || 0), max = Math.max(1, ...vals), iMax = vals.indexOf(Math.max(...vals)), todos = pontos.length <= 8;
  return `<div class="card"><div class="colunas">${pontos.map((p, i) => {
    const v = vals[i], pico = i === iMax && v > 0;
    return `<div class="c ${pico ? "pico" : ""}">${(todos || pico) && v > 0 ? `<em>${fmt(v)}</em>` : ""}<i style="height:${Math.max(1.5, v / max * 100)}%"></i></div>`;
  }).join("")}</div><div class="colunas-rot">${pontos.map(p => `<span>${esc(p[0])}</span>`).join("")}</div></div>`;
}

// horário × funcionário (23/10/2026, Ronan: "coloca em linha com o nome, horário e quantidade"): tabela com uma linha por
// vendedor e uma coluna por hora, em litros. O nome fica parado à esquerda e as horas correm de lado; o maior número de cada
// vendedor vai em negrito (o pico dele), e a última linha soma a hora.
function eqvTabelaHoras(vend, ini, fim) {
  const horas = [];
  for (let h = ini; h <= fim; h++) horas.push(h);
  const tot = horas.map(h => vend.reduce((s, v) => s + (v[1][h] || 0), 0));
  const cel = (x, pico) => (x > 0 ? `<td class="${pico ? "pico" : ""}">${num(Math.round(x))}</td>` : '<td class="z">–</td>');
  return `<div class="card tab-horas"><div class="rolar"><table>
    <thead><tr><th class="n">Funcionário</th>${horas.map(h => `<th>${String(h).padStart(2, "0")}h</th>`).join("")}<th class="tot">Total</th></tr></thead>
    <tbody>${vend.map(v => {
      const pico = Math.max(...horas.map(h => v[1][h] || 0));
      return `<tr><th class="n">${esc(v[0])}</th>${horas.map(h => cel(v[1][h] || 0, (v[1][h] || 0) === pico)).join("")}<td class="tot">${num(Math.round(v[2]))}</td></tr>`;
    }).join("")}</tbody>
    <tfoot><tr><th class="n">Total</th>${tot.map(x => `<td>${x > 0 ? num(Math.round(x)) : "–"}</td>`).join("")}<td class="tot">${num(Math.round(vend.reduce((s, v) => s + v[2], 0)))}</td></tr></tfoot>
  </table></div></div>`;
}

// ================================================================ VENDAS
async function telaEqVendas() {
  const eu = await eqEu();
  if (!eu) return;
  const p = eqvPeriodo();
  const r = await apiEqRapido("/app/equipe/api/vendas?" + eqvQuery(p), telaEqVendas, () => eqvCarregando("Vendas", false, telaEqVendas));
  if (!r.ok) { eqvErro("Vendas", false, r, telaEqVendas); return; }
  EQV.postos = r.postos || EQV.postos;
  const c = r.combustivel || {}, pr = r.produtos || {};
  const temR = _n(r.total_valor), temL = _n(c.litros);
  const varios = _dDias(p.de, p.ate) > 1;
  const sec = (tit, corpo, extra) => corpo ? `<div class="secao"><div class="secao-tit"><h2>${tit}</h2>${extra || ""}</div>${corpo}</div>` : "";

  const heroi = temR ? `<div class="card heroi"><div class="rot">Faturamento ${p.de === p.ate ? "do dia" : "no período"}</div><div class="val">${brl(r.total_valor)}</div>
      <div class="s">${[_n(c.valor) ? "Combustível " + brl(c.valor) : "", _n(pr.valor) ? "Loja " + brl(pr.valor) : ""].filter(Boolean).join(" · ")}</div></div>`
    : temL ? `<div class="card heroi"><div class="rot">Litros ${p.de === p.ate ? "do dia" : "no período"}</div><div class="val">${_l(c.litros)}</div></div>` : "";

  const combGrade = _grade([
    temL && _celula("Litros", _l(c.litros)),
    _n(c.veiculos) && _celula("Veículos", num(c.veiculos)),
    _n(c.ticket_litros) && _celula("Ticket médio", (Math.round(c.ticket_litros * 100) / 100).toLocaleString("pt-BR") + " L"),
    _n(c.ticket_valor) && _celula("Ticket médio", brl(c.ticket_valor)),
    varios && _n(c.media_dia_litros) && _celula("Média por dia", _l(c.media_dia_litros)),
    _n(c.previsao_mes_litros) && _celula("Previsão do mês", _l(c.previsao_mes_litros), "no ritmo de " + _l(c.media_mes_litros) + "/dia"),
  ]);
  const porComb = (c.por_combustivel || []).filter(x => (x[1] || 0) > 0 || (x[2] || 0) > 0);
  const combBarras = porComb.length ? eqvBarras(porComb.map(x => [x[0], temL ? x[1] : x[2],
    temL ? _l(x[1]) : brl(x[2]), temL && _n(x[2]) ? brl(x[2]) : ""]), true) : "";
  const horas = (c.por_hora || []).map(x => [String(x[0]), x[1]]);
  const comHora = horas.filter(h => h[1] > 0);
  let colHoras = "";
  if (comHora.length) {
    const ini = Math.max(0, Math.min(...comHora.map(h => +h[0])) - 1), fim = Math.min(23, Math.max(...comHora.map(h => +h[0])) + 1);
    const faixa = horas.filter(h => +h[0] >= ini && +h[0] <= fim).map(h => [(+h[0] % 3 === 0 ? h[0] + "h" : ""), h[1]]);
    const pico = comHora.reduce((m, h) => (h[1] > m[1] ? h : m), comHora[0]);
    colHoras = eqvColunas(faixa, v => num(Math.round(v)) + " L") + `<div class="vazio" style="padding:6px 2px 0">Pico às ${pico[0]}h. Litros abastecidos em cada hora${varios ? ", somando os dias do período" : ""}.</div>`;
  }
  const vendHora = (c.por_vendedor_hora || []).filter(v => _n(v[2]));
  let mapa = "";
  if (vendHora.length && comHora.length) {
    const ini = Math.min(...comHora.map(h => +h[0])), fim = Math.max(...comHora.map(h => +h[0]));
    mapa = eqvTabelaHoras(vendHora, ini, fim) + `<div class="vazio" style="padding:6px 2px 0">Litros de cada funcionário (crachá na bomba) em cada hora${varios ? ", somando os dias do período" : ""}. Arraste a tabela para o lado para ver todas as horas; em negrito, a hora em que cada um mais vendeu.</div>`;
  }
  const prodGrade = _grade([
    _n(pr.qtd) && _celula("Itens vendidos", num(pr.qtd)),
    _n(pr.valor) && _celula("Valor", brl(pr.valor)),
    _n(pr.ticket) && _celula("Ticket médio", brl(pr.ticket)),
    _n(pr.participacao) && _celula("Do faturamento", _pct(pr.participacao)),
  ]);
  const top = (pr.top || []).length ? eqvBarras(pr.top.map(x => [x[0], x[2] || x[1], _n(x[2]) ? brl(x[2]) : num(x[1]) + " un", _n(x[2]) ? num(x[1]) + " un" : ""])) : "";
  const formas = (r.formas || []).filter(f => f[1] > 0);
  const formasB = formas.length ? eqvBarras(formas.map(f => [f[0], f[1], brl(f[1]), temR && r.total_valor ? _pct(f[1] / r.total_valor * 100) : ""])) : "";

  el().innerHTML = `<div class="tela">${topoSimples("Vendas", false)}${eqvFiltros(telaEqVendas)}
    <div class="secao" style="padding-top:12px">${heroi || '<div class="card vazio" style="padding:16px">Seu perfil não vê valores de venda.</div>'}</div>
    ${sec("Combustível", combGrade)}
    ${sec("Por combustível", combBarras)}
    ${sec("Por horário", colHoras)}
    ${sec("Por horário e funcionário", mapa)}
    ${sec("Loja", prodGrade + (top ? '<div style="height:10px"></div>' + top : ""), top ? '<span style="color:var(--mut);font-size:.8rem">mais vendidos</span>' : "")}
    ${sec("Formas de pagamento", formasB)}
    <div class="secao"><div class="blocos">
      <button class="card bloco" onclick="ir('#/eq-comparativo')"><span class="ic">${ICO.comparar(20)}</span><div class="t">Comparar períodos</div><div class="s">Este período contra outro</div></button>
      ${eqPode("ver_tanques") ? `<button class="card bloco" onclick="ir('#/eq-estoque')"><span class="ic">${ICO.tanque(20)}</span><div class="t">Estoque</div><div class="s">Sistema × medidor do tanque</div></button>` : ""}
    </div></div>
  </div>`;
}

// ================================================================ GESTÃO (atalhos)
async function telaEqGestao() {
  const eu = await eqEu();
  if (!eu) return;
  const b = (rota, ic, t, s) => `<button class="card bloco" onclick="ir('#/${rota}')"><span class="ic">${ICO[ic](20)}</span><div class="t">${t}</div><div class="s">${s}</div></button>`;
  const itens = [
    eqVePostos() && b("eq-postos", "loja", "Postos agora", "Turno, tanques, recebido e preço na bomba"),
    (eqPode("ver_vendas") || eqPode("ver_litros")) && b("eq-vendas", "grafico", "Vendas", "Por período, combustível, horário e loja"),
    (eqPode("ver_vendas") || eqPode("ver_litros")) && b("eq-comparativo", "comparar", "Comparar períodos", "Litros, faturamento e tickets lado a lado"),
    eqPode("ver_tanques") && b("eq-estoque", "tanque", "Estoque", "Sistema × medidor e quanto ainda cabe"),
    eqPode("ver_tanques") && b("eq-descargas", "caminhao", "Descargas", "Cada entrega que o medidor registrou no tanque"),
    eqPode("ver_comissao") && b("eq-comissao", "moeda", "Comissão", "Vendas e comissão de cada vendedor"),
  ].filter(Boolean);
  el().innerHTML = `<div class="tela">${topoSimples("Gestão", false)}
    <div class="secao" style="padding-top:2px">${itens.length ? `<div class="blocos">${itens.join("")}</div>` : '<div class="card vazio" style="padding:16px">Nada liberado para o seu perfil aqui.</div>'}
      <div class="vazio" style="text-align:center;padding-top:16px">Em breve aqui: financeiro, auditoria, ranking da equipe e metas.</div></div>
  </div>`;
}

// ================================================================ COMPARAR PERÍODOS
function eqvPeriodoCmp(p) {
  const dias = _dDias(p.de, p.ate);
  if (EQV.cmp === "custom" && EQV.cde && EQV.cate) return { de: EQV.cde, ate: EQV.cate };
  if (EQV.cmp === "ano") return { de: (+p.de.slice(0, 4) - 1) + p.de.slice(4), ate: (+p.ate.slice(0, 4) - 1) + p.ate.slice(4) };
  if (EQV.cmp === "mes") {
    const m = iso => { const d = new Date(iso + "T12:00:00"), dia = d.getDate(); d.setDate(1); d.setMonth(d.getMonth() - 1);
      d.setDate(Math.min(dia, new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate())); return _dIso(d); };
    return { de: m(p.de), ate: m(p.ate) };
  }
  return { de: _dMais(p.de, -dias), ate: _dMais(p.de, -1) };       // o período anterior, do mesmo tamanho
}
async function telaEqComparativo() {
  const eu = await eqEu();
  if (!eu) return;
  const a = eqvPeriodo(), b = eqvPeriodoCmp(a);
  const r = await apiEqRapido("/app/equipe/api/comparativo?" + eqvQuery(a) + "&cde=" + b.de + "&cate=" + b.ate, telaEqComparativo,
    () => eqvCarregando("Comparar períodos", true, telaEqComparativo));
  if (!r.ok) { eqvErro("Comparar períodos", true, r, telaEqComparativo); return; }
  EQV.postos = r.postos || EQV.postos;
  const A = r.a || {}, B = r.b || {};
  const fL = v => _l(v), fR = v => brl(v), fN = v => num(Math.round(v)), fT = v => (Math.round(v * 100) / 100).toLocaleString("pt-BR") + " L";
  const linhas = [["Litros", "litros", fL], ["Faturamento", "valor", fR], ["Veículos", "veiculos", fN], ["Ticket médio (L)", "ticket_litros", fT],
    ["Ticket médio (R$)", "ticket_valor", fR], ["Itens da loja", "prod_qtd", fN], ["Loja (R$)", "prod_valor", fR], ["Ticket da loja", "prod_ticket", fR]]
    .filter(l => _n(A[l[1]]) || _n(B[l[1]]));
  const vari = (x, y) => {
    if (!_n(x) || !_n(y) || !y) return '<span class="var igual">—</span>';
    const pc = Math.round((x - y) / y * 1000) / 10, cls = Math.abs(pc) < 0.5 ? "igual" : pc > 0 ? "sobe" : "desce";
    return `<span class="var ${cls}">${pc > 0 ? "+" : ""}${(pc === 0 ? 0 : pc).toLocaleString("pt-BR")}%</span>`;
  };
  const CMP = [["anterior", "Período anterior"], ["mes", "Mês anterior"], ["ano", "Ano anterior"], ["custom", "Escolher"]];
  el().innerHTML = `<div class="tela">${topoSimples("Comparar períodos", true)}${eqvFiltros(telaEqComparativo)}
    <div class="secao" style="padding-top:12px"><div class="secao-tit"><h2>Comparar com</h2></div>
      <div class="chips" style="padding-top:0">${CMP.map(x => `<button class="chip ${EQV.cmp === x[0] ? "on" : ""}" onclick="${x[0] === "custom" ? "eqvEscolherDatas('cmp')" : `EQV.cmp='${x[0]}';telaEqComparativo()`}">${x[1]}</button>`).join("")}</div>
      <div class="per-txt">${esc(eqvRotulo(b))}</div></div>
    <div class="secao">${linhas.length ? `<div class="card cmp3">
      <div class="cmp3-cab"><span>${esc(_dBr(a.de) + (a.de !== a.ate ? " a " + _dBr(a.ate) : ""))}</span><span>${esc(_dBr(b.de) + (b.de !== b.ate ? " a " + _dBr(b.ate) : ""))}</span><span>Variação</span></div>
      ${linhas.map(l => `<div class="cmp3-l"><div class="rot">${l[0]}</div><div class="nums"><b>${_n(A[l[1]]) ? l[2](A[l[1]]) : "—"}</b><span>${_n(B[l[1]]) ? l[2](B[l[1]]) : "—"}</span>${vari(A[l[1]], B[l[1]])}</div></div>`).join("")}</div>
      <div class="vazio" style="padding:6px 2px 0">Variação: quanto o período escolhido (1ª coluna) subiu ou caiu em relação ao comparado (2ª coluna).</div>`
      : '<div class="card vazio" style="padding:16px">Sem vendas nos dois períodos.</div>'}</div>
    ${eqPode("ver_litros") ? `<div class="secao" id="cmp-meses"><div class="secao-tit"><h2>Litros por mês</h2></div><div class="sk" style="height:150px"></div></div>` : ""}
  </div>`;
  eqvMeses();
}
// o gráfico dos últimos meses vem à parte (é a consulta pesada): a tabela aparece antes
async function eqvMeses() {
  const caixa = document.getElementById("cmp-meses");
  if (!caixa) return;
  const r = await apiEqRapido("/app/equipe/api/meses" + (EQV.posto ? "?posto=" + encodeURIComponent(EQV.posto) : ""), eqvMeses);
  const alvo = document.getElementById("cmp-meses");
  if (!alvo) return;                                   // saiu da tela antes de chegar
  const meses = (r.ok && Array.isArray(r.meses) ? r.meses : []).filter(m => _n(m[1]));
  const nomeMes = ym => ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"][(+String(ym).slice(5, 7) || 1) - 1];
  if (!meses.length) { alvo.remove(); return; }
  alvo.innerHTML = `<div class="secao-tit"><h2>Litros por mês</h2></div>${eqvColunas(meses.map(m => [nomeMes(m[0]), m[1]]), v => num(Math.round(v / 1000)) + " mil")}
    <div class="vazio" style="padding:6px 2px 0">O mês atual vai até hoje.</div>`;
}

// AO VIVO (23/10/2026, Ronan: "quando descarrega combustível precisamos ver a variação em tempo real"): Estoque e Descargas
// pedem de novo a cada 15 s enquanto a tela está aberta e o app na frente (a sonda lê a cada ~60 s; o servidor guarda 15 s).
let EQV_VIVO = null;
function eqvAoVivo(rota, fn) {
  if (EQV_VIVO && EQV_VIVO.rota === rota) return;
  if (EQV_VIVO) clearInterval(EQV_VIVO.t);
  const vivo = { rota };
  vivo.t = setInterval(() => {
    if (location.hash.split("?")[0] !== "#/" + rota) { clearInterval(vivo.t); if (EQV_VIVO === vivo) EQV_VIVO = null; return; }
    if (!document.hidden) fn();
  }, 15000);
  EQV_VIVO = vivo;
}
const eqvAoVivoTxt = '<div class="vivo-txt"><i></i>Atualiza sozinho a cada 15 s</div>';
// "Descarga em andamento": subida do volume no medidor desde `desde` (o registro da descarga só chega quando ela termina)
function eqvDescarregando(d, posto) {
  return `<div class="descarregando"><div class="cab"><b><i></i>Descarga em andamento</b><span>+${_l(d.litros)}</span></div>
    <div class="s">${posto ? esc(posto) + " · " : ""}desde ${horaBr(d.desde)} · tanque agora com ${_l(d.volume)}</div></div>`;
}

// ================================================================ ESTOQUE: sistema × medidor
async function telaEqEstoque() {
  const eu = await eqEu();
  if (!eu) return;
  const r = await apiEqRapido("/app/equipe/api/estoque" + (EQV.posto ? "?posto=" + encodeURIComponent(EQV.posto) : ""), telaEqEstoque,
    () => eqvCarregando("Estoque", true, telaEqEstoque, true));
  if (!r.ok) { eqvErro("Estoque", true, r, telaEqEstoque); return; }
  EQV.postos = r.postos || EQV.postos;
  const postos = Array.isArray(r.estoque) ? r.estoque : [];
  const tanque = t => {
    const pct = Math.max(0, Math.min(100, Math.round(t.pct || 0)));
    const dif = _n(t.sistema) && _n(t.medidor) ? t.medidor - t.sistema : null;
    const forte = dif != null && t.capacidade && Math.abs(dif) > t.capacidade * 0.03;
    return `<div class="tanque-linha est ${t.baixo ? "baixo" : ""}"><div class="cab"><span>${esc(t.produto)}${t.numero ? ` <small style="color:var(--mut);font-weight:500">· tanque ${esc(t.numero)}</small>` : ""}</span><span class="dir">${_n(t.medidor) ? _l(t.medidor) : "—"}</span></div>
      <div class="nivel"><i style="width:${pct}%"></i></div>
      <div class="difs"><span>Sistema <b>${_n(t.sistema) ? _l(t.sistema) : "—"}</b></span><span class="${forte ? "alerta" : ""}">Diferença <b>${dif == null ? "—" : (dif > 0 ? "+" : "") + num(Math.round(dif)) + " L"}</b></span><span>Cabe <b>${_n(t.cabe) ? _l(t.cabe) : "—"}</b></span></div>
      ${_n(t.abertura) ? `<div class="difs" style="margin-top:2px"><span>Abriu com ${_l(t.abertura)} · vendeu ${_l(t.vendido)}${t.entrou ? ` · descarga +${_l(t.entrou)}` : ""}</span></div>` : ""}
      ${t.descarregando ? eqvDescarregando(t.descarregando) : ""}</div>`;
  };
  eqvAoVivo("eq-estoque", telaEqEstoque);
  el().innerHTML = `<div class="tela">${topoSimples("Estoque", true)}${eqvFiltros(telaEqEstoque, true)}${eqvAoVivoTxt}
    ${postos.length ? postos.map(p => `<div class="secao"><div class="secao-tit"><h2>${esc(p.nome)}</h2><span style="color:var(--mut);font-size:.8rem">${esc(p.medido_em ? "medidor às " + horaBr(p.medido_em) : "sem leitura do medidor")}</span></div>
      <div class="card">${(p.tanques || []).map(tanque).join("") || '<div class="vazio" style="padding:14px 16px">Nenhum tanque cadastrado.</div>'}</div></div>`).join("")
      : '<div class="secao"><div class="card vazio" style="padding:16px">Nenhum posto liberado para o seu perfil.</div></div>'}
    <div class="secao"><button class="card bloco" style="width:100%;min-height:0;flex-direction:row;align-items:center" onclick="ir('#/eq-descargas')"><span class="ic" style="margin:0">${ICO.caminhao(20)}</span>
      <div style="flex:1"><div class="t">Descargas</div><div class="s">Cada entrega que o medidor registrou no tanque</div></div><span class="seta">${ICO.seta(18)}</span></button></div>
    <div class="secao"><div class="vazio" style="padding-top:0"><b>Sistema</b> é o estoque do livro no dia, como no LMC: a medição da abertura, mais as descargas, menos o que as bombas daquele tanque venderam hoje. <b>Medidor</b> é a última leitura da sonda. A diferença (perda ou sobra do dia) fica em vermelho quando passa de 3% do tanque. <b>Cabe</b> é quanto ainda entra até a capacidade.</div></div>
  </div>`;
}

// ================================================================ DESCARGAS (17/10/2026)
// Pedido do Ronan: "histórico de descarga de combustível para ficar mais fácil identificar se houve ou não a entrega" — só a
// variação identificada no medidor (sem casar com a nota): cada subida de volume que a sonda do tanque registrou.
function _diaLongo(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString("pt-BR", { weekday: "long", day: "2-digit", month: "2-digit" }).replace(/^./, c => c.toUpperCase());
}
async function telaEqDescargas() {
  const eu = await eqEu();
  if (!eu) return;
  const p = eqvPeriodo();
  const r = await apiEqRapido("/app/equipe/api/descargas?" + eqvQuery(p), telaEqDescargas, () => eqvCarregando("Descargas", true, telaEqDescargas));
  if (!r.ok) { eqvErro("Descargas", true, r, telaEqDescargas); return; }
  EQV.postos = r.postos || EQV.postos;
  const ds = Array.isArray(r.descargas) ? r.descargas : [], rs = r.resumo || {}, varios = (EQV.postos || []).length > 1 && !EQV.posto;
  const descarga = d => `<div class="linha"><span class="ic">${ICO.caminhao(20)}</span>
    <div class="meio"><div class="t">${esc(d.produto)} · tanque ${esc(d.tanque)}</div>
      <div class="s">${horaBr(d.inicio)}${d.fim ? "–" + horaBr(d.fim) : ""}${varios ? " · " + esc(d.posto) : ""}</div>
      <div class="s">de ${_l(d.antes)} para ${_l(d.depois)}</div></div>
    <div class="dir">+${_l(d.litros)}</div></div>`;
  const porDia = {};
  ds.forEach(d => { const k = new Date(d.inicio).toLocaleDateString("pt-BR"); (porDia[k] = porDia[k] || []).push(d); });
  const agora = Array.isArray(r.agora) ? r.agora : [];
  eqvAoVivo("eq-descargas", telaEqDescargas);
  el().innerHTML = `<div class="tela">${topoSimples("Descargas", true)}${eqvFiltros(telaEqDescargas)}${eqvAoVivoTxt}
    ${agora.length ? `<div class="secao" style="padding-top:12px"><div class="secao-tit"><h2>Agora</h2></div>
      <div class="card">${agora.map(a => `<div style="padding:12px 16px 4px"><div class="t" style="font-weight:700">${esc(a.produto)} · tanque ${esc(a.tanque)}</div>${eqvDescarregando(a, varios ? a.posto : "")}</div>`).join("")}</div></div>` : ""}
    <div class="secao" style="padding-top:12px"><div class="card heroi"><div class="rot">Combustível descarregado</div>
      <div class="val">${_l(rs.litros)}</div><div class="s">${num(rs.descargas || 0)} ${rs.descargas === 1 ? "descarga" : "descargas"} no período</div></div></div>
    ${Object.keys(porDia).length ? Object.keys(porDia).map(k => `<div class="secao"><div class="secao-tit"><h2 style="font-size:.95rem">${esc(_diaLongo(porDia[k][0].inicio))}</h2></div>
      <div class="card lista">${porDia[k].map(descarga).join("")}</div></div>`).join("")
      : '<div class="secao"><div class="card vazio" style="padding:16px">Nenhuma descarga no período.</div></div>'}
    ${(r.sem_sonda || []).length ? `<div class="secao"><div class="vazio">Sem registro da sonda: ${esc(r.sem_sonda.join(", "))}.</div></div>` : ""}
    <div class="secao"><div class="vazio" style="padding-top:0">Cada linha é uma subida de volume que a sonda do tanque registrou (console do medidor), com o volume antes e depois.</div></div>
  </div>`;
}

// ================================================================ COMISSÃO (tela pronta; a regra entra depois)
async function telaEqComissao() {
  const eu = await eqEu();
  if (!eu) return;
  const p = eqvPeriodo();
  const r = await apiEqRapido("/app/equipe/api/comissao?" + eqvQuery(p), telaEqComissao, () => eqvCarregando("Comissão", true, telaEqComissao));
  if (!r.ok) { eqvErro("Comissão", true, r, telaEqComissao); return; }
  EQV.postos = r.postos || EQV.postos;
  const vs = Array.isArray(r.vendedores) ? r.vendedores : [];
  // 23/10/2026, Ronan: a comissão é o % da venda (tela Comissões do retaguarda, por posto), não o valor vendido. Hoje 0% = R$ 0,00.
  const pcts = Array.isArray(r.pct) ? r.pct : [], difs = [...new Set(pcts.map(x => Number(x[1]) || 0))];
  const pctTxt = !pcts.length ? "" : difs.length === 1 ? _pct(difs[0]) + " da venda de combustível"
    : pcts.map(x => esc(String(x[0]).replace(/^Posto /i, "")) + " " + _pct(x[1])).join(" · ");
  const zerada = difs.length === 1 && difs[0] === 0;
  el().innerHTML = `<div class="tela">${topoSimples("Comissão", true)}${eqvFiltros(telaEqComissao)}
    <div class="secao" style="padding-top:12px"><div class="card heroi"><div class="rot">Comissão no período</div><div class="val">${brl(r.total || 0)}</div>
      <div class="s">${pctTxt}</div></div></div>
    ${zerada ? `<div class="secao"><div class="aviso-info"><span class="ic">${ICO.moeda(18)}</span><div>O percentual de comissão do combustível está em 0%, por isso a comissão sai zerada. Ele é definido no retaguarda, em Comissões.</div></div></div>` : ""}
    <div class="secao"><div class="secao-tit"><h2>Vendedores</h2><span style="color:var(--mut);font-size:.8rem">${num(vs.length)}</span></div>
      ${vs.length ? `<div class="card lista">${vs.map((v, i) => `<div class="linha"><span class="rank ${i === 0 ? "r1" : ""}">${i + 1}</span>
        <div class="meio"><div class="t">${esc(v.nome)}</div><div class="s">${[_n(v.litros) ? _l(v.litros) : "", _n(v.abastecimentos) ? num(v.abastecimentos) + " abastec." : "", v.posto ? esc(v.posto) : ""].filter(Boolean).join(" · ")}</div></div>
        <div class="dir">${brl(v.comissao || 0)}<div style="font-size:.76rem;font-weight:500;color:var(--mut)">${_n(v.valor) ? "vendeu " + brl(v.valor) : ""}</div></div></div>`).join("")}</div>`
        : '<div class="card vazio" style="padding:16px">Nenhuma venda com vendedor identificado neste período.</div>'}
      <div class="vazio">O vendedor é quem estava com o crachá na bomba (identificador do frentista).${r.sem_cracha ? ` ${num(r.sem_cracha.abastecimentos)} abastecimento(s) (${_l(r.sem_cracha.litros)}) sem crachá não entram na lista.` : ""}</div></div>
  </div>`;
}

Object.assign(TELAS, { "eq-vendas": telaEqVendas, "eq-gestao": telaEqGestao, "eq-comparativo": telaEqComparativo, "eq-estoque": telaEqEstoque,
  "eq-comissao": telaEqComissao, "eq-descargas": telaEqDescargas });
