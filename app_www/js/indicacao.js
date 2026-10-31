// ============================================================
// App Postos SN — INDIQUE UM AMIGO (31/10/2026)
// ------------------------------------------------------------
// Pedido do Ronan: quem indica ganha o desconto do app DOBRADO no próximo abastecimento; "na tela dele aparecer um
// indicativo dizendo que o amigo (nome) finalizou o cadastro e o próximo desconto será (valor x2); depois de usar
// esse desconto volta ao desconto normal". O bônus libera no 1º abastecimento do amigo pelo app (só o cadastro é
// fácil de forjar). Quem conta é o servidor (app_indicacao.py):
//   * card no Início (#ind-card): bônus liberado > amigo que terminou o cadastro > convite;
//   * tela #/indicar: código, enviar pelo WhatsApp / copiar, os amigos e "alguém te convidou?";
//   * cadastro: campo "Código de convite" só com a campanha valendo (indCampoCadastro);
//   * bico: bomba.js mostra o desconto multiplicado quando /cashback/api/desconto traz "bonus".
// ============================================================

ICO.amigos = t => `<svg width="${t || 24}" height="${t || 24}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8"/><path d="M18.5 14a6.5 6.5 0 0 1 3 6"/></svg>`;

function indVezes(r) { return Number(r.multiplicador || 2) === 2 ? "em dobro" : "x" + String(r.multiplicador).replace(".", ","); }

// "R$ 0,15 → R$ 0,30 por litro no Etanol Comum (Posto Seven BH)" — um por combustível com desconto do app
function indLinhasDesconto(r) {
  return (r.descontos || []).map(e => `<div class="ind-desc">⛽ <b>${esc(e.combustivel)}</b> no ${esc(e.posto)}: <s>${brl(e.desconto)}</s> → <b>${brl(e.dobrado)}</b> de desconto por litro</div>`).join("");
}

function indNomes(lst) {
  const n = lst.map(a => a.nome);
  return n.length === 1 ? n[0] : n.length === 2 ? n[0] + " e " + n[1] : `${n[0]} e mais ${n.length - 1} amigos`;
}

// ---------------------------------------------------------------- card do Início e item do Perfil
async function indicacaoPreencher() {
  try {
    const r = await api("/cashback/api/indicacao");
    if (!r.ok) return;
    S.ind = r;
    const per = document.getElementById("ind-perfil");
    if (per && (r.ativo || (r.amigos || []).length)) {
      per.innerHTML = `<div class="card lista" style="margin-top:10px"><button class="linha" onclick="ir('#/indicar')"><span class="ic">${ICO.amigos(20)}</span>
        <div class="meio"><div class="t">Indicar amigos</div><div class="s">${r.disponiveis ? `${r.disponiveis} desconto${r.disponiveis > 1 ? "s" : ""} ${indVezes(r)} para usar` : "Ganhe o desconto do app " + indVezes(r)}</div></div>
        <span class="seta">${ICO.seta(18)}</span></button></div>`;
    }
    const d = document.getElementById("ind-card");
    if (!d) return;
    const esperando = (r.amigos || []).filter(a => a.status === "cadastrado");
    const liberou = (r.amigos || []).filter(a => a.bonus === "disponivel");
    let html = "";
    if (r.disponiveis > 0) {
      const quem = liberou.length ? `${esc(indNomes(liberou))} ${liberou.length > 1 ? "fizeram" : "fez"} o primeiro abastecimento pelo app.`
        : r.ganhou_como_amigo ? "Presente de boas-vindas por entrar com um convite." : "";
      html = `<button class="card ind-card ouro" onclick="ir('#/indicar')"><span class="ic">🎁</span><div class="meio">
        <div class="t">Desconto ${indVezes(r)} liberado!</div>
        <div class="s">${quem} ${r.disponiveis > 1 ? `Os seus próximos <b>${r.disponiveis} abastecimentos</b>` : "O seu <b>próximo abastecimento</b>"} com o desconto do app ${r.disponiveis > 1 ? "saem" : "sai"} com o desconto ${indVezes(r)}:</div>
        ${indLinhasDesconto(r)}
        <div class="s" style="margin-top:4px">Depois volta ao desconto normal.</div></div></button>`;
    } else if (r.reservado) {
      html = `<div class="card ind-card ouro"><span class="ic">⛽</span><div class="meio"><div class="t">Seu desconto ${indVezes(r)} está no bico</div>
        <div class="s">Ele só é gasto quando o abastecimento acontece. Se não abastecer, ele volta para você.</div></div></div>`;
    } else if (esperando.length) {
      html = `<button class="card ind-card" onclick="ir('#/indicar')"><span class="ic">👋</span><div class="meio">
        <div class="t">${esc(indNomes(esperando))} ${esperando.length > 1 ? "terminaram" : "terminou"} o cadastro com o seu convite</div>
        <div class="s">Quando ${esperando.length > 1 ? "um deles fizer" : esc(esperando[0].nome) + " fizer"} o primeiro abastecimento pelo app, o seu próximo desconto sai ${indVezes(r)}:</div>
        ${indLinhasDesconto(r)}</div></button>`;
    } else if (r.ativo) {
      html = `<button class="card ind-card" onclick="ir('#/indicar')"><span class="ic">${ICO.amigos(22)}</span><div class="meio">
        <div class="t">Indique um amigo e ganhe o desconto ${indVezes(r)}</div>
        <div class="s">Seu amigo baixa o app e cria a conta com o seu código. No primeiro abastecimento dele, o seu próximo desconto sai ${indVezes(r)}.</div></div>
        <span class="seta">${ICO.seta(18)}</span></button>`;
    }
    d.innerHTML = html ? `<div class="secao" style="padding-bottom:0">${html}</div>` : "";
  } catch (e) { /* a indicação é opcional: se falhar, o resto do Início segue */ }
}

// ---------------------------------------------------------------- tela Indicar amigos
function indMensagem(r) {
  return `Baixe o app Postos SN e crie a sua conta com o meu código de convite *${r.codigo}*. ` +
    (r.amigo_ganha ? "O seu primeiro desconto do app já sai em dobro! " : "Com o app você tem desconto no abastecimento e ganha pontos. ") +
    (r.link || "https://www.redesn.com.br/baixar");
}

const IND_SITUACAO = {
  cadastrado: ["Terminou o cadastro", "Falta o primeiro abastecimento pelo app", "var(--azul-2)"],
  disponivel: ["Abasteceu", "Seu desconto em dobro está liberado", "var(--verde)"],
  reservado: ["Abasteceu", "Seu desconto em dobro está no bico agora", "var(--verde)"],
  usado: ["Abasteceu", "Desconto em dobro já usado", "var(--mut)"],
  recusada: ["Não valeu", "O convite não vale no mesmo celular de quem indicou", "var(--verm)"],
};

async function telaIndicar() {
  el().innerHTML = `<div class="tela">${topoSimples("Indicar amigos", true)}<div class="secao"><div class="card vazio" style="padding:16px">Carregando…</div></div></div>`;
  const r = await api("/cashback/api/indicacao");
  if (r.erro && /sess/.test(r.erro)) { sessaoCaiu(); return; }
  if (!r.ok) throw new Error(r.erro || "Não consegui abrir a indicação.");
  S.ind = r;
  const amigos = r.amigos || [];
  const sit = a => IND_SITUACAO[a.status === "abasteceu" ? (a.bonus || "usado") : a.status] || IND_SITUACAO.cadastrado;
  el().innerHTML = `<div class="tela">${topoSimples("Indicar amigos", true)}
    ${r.ativo ? `<div class="secao" style="padding-top:4px"><div class="card ind-topo">
      <div class="ind-gde">Ganhe o desconto do app ${indVezes(r)}</div>
      <div class="s">Seu amigo baixa o app Postos SN e cria a conta com o seu código. Quando ele fizer o <b>primeiro abastecimento pelo app</b>, o seu próximo abastecimento com o desconto do app sai com o desconto ${indVezes(r)}.</div>
      ${indLinhasDesconto(r)}
      <div class="ind-cod-rot">Seu código de convite</div>
      <div class="ind-cod" id="ind-cod">${esc(r.codigo || "")}</div>
      <button class="btn ind-zap" onclick="indEnviarZap()">Enviar pelo WhatsApp</button>
      <div class="duas" style="margin-top:10px">
        <button class="btn sec" onclick="indCopiar()">Copiar o código</button>
        <button class="btn sec" id="ind-share" onclick="indCompartilhar()">Compartilhar</button>
      </div></div></div>`
    : `<div class="secao" style="padding-top:4px"><div class="card vazio" style="padding:16px">A campanha de indicação foi encerrada. Os descontos ${indVezes(r)} que você já ganhou continuam valendo.</div></div>`}
    ${r.disponiveis ? `<div class="secao" style="padding-top:0"><div class="card ind-card ouro"><span class="ic">🎁</span><div class="meio">
      <div class="t">${r.disponiveis} desconto${r.disponiveis > 1 ? "s" : ""} ${indVezes(r)} para usar</div>
      <div class="s">Use no bico: leia o QR ou digite o código e escolha <b>Desconto agora</b>. Cada um vale um abastecimento.</div></div></div></div>` : ""}
    <div class="secao" style="padding-top:0"><div class="secao-tit"><h2>Seus amigos</h2></div>
      ${amigos.length ? `<div class="card lista">${amigos.map(a => `<div class="linha"><span class="ic">${ICO.amigos(20)}</span>
        <div class="meio"><div class="t">${esc(a.nome)}</div><div class="s">${esc(sit(a)[1])} · desde ${esc(dataBr(a.quando))}</div></div>
        <div class="dir" style="color:${sit(a)[2]};font-size:.8rem;font-weight:700;text-align:right">${esc(sit(a)[0])}</div></div>`).join("")}</div>`
        : `<div class="card vazio" style="padding:16px">Nenhum amigo ainda. Mande o seu código pelo WhatsApp!</div>`}
    </div>
    ${r.pode_digitar ? `<div class="secao" style="padding-top:0"><div class="card" style="padding:16px">
      <div class="t" style="font-weight:700">Alguém convidou você?</div>
      <div class="s" style="color:var(--mut);font-size:.86rem">Digite o código de quem convidou. Vale para conta nova, antes do primeiro abastecimento pelo app.</div>
      <div class="campo"><input id="ind-outro" autocapitalize="characters" autocomplete="off" placeholder="Ex.: MARIA123" maxlength="16"></div>
      <button class="btn sec" style="margin-top:10px" onclick="indUsarCodigo()">Usar o código</button>
      <div class="msg" id="ind-msg"></div></div></div>` : ""}
    <div class="secao" style="padding-top:0"><div class="vazio" style="padding-top:0">Como funciona: cada amigo vale <b>um</b> abastecimento com o desconto ${indVezes(r)}, para você usar quando quiser. Vale onde o desconto do app vale (posto, combustível e forma de pagamento) e só é gasto quando o abastecimento acontece. Não vale indicar a si mesmo nem usar o mesmo celular.</div></div>
  </div>`;
  if (!navigator.share) { const b = document.getElementById("ind-share"); if (b) b.style.display = "none"; }
}

function indEnviarZap() {
  if (!S.ind) return;
  window.open("https://wa.me/?text=" + encodeURIComponent(indMensagem(S.ind)), "_blank");
}

async function indCopiar() {
  if (!S.ind) return;
  const txt = S.ind.codigo || "";
  try { await navigator.clipboard.writeText(txt); toast("Código copiado: " + txt); }
  catch (e) {
    const i = document.createElement("input"); i.value = txt; document.body.appendChild(i); i.select();
    try { document.execCommand("copy"); toast("Código copiado: " + txt); } catch (e2) { toast("Seu código: " + txt); }
    i.remove();
  }
}

async function indCompartilhar() {
  if (!S.ind || !navigator.share) return;
  try { await navigator.share({ title: "App Postos SN", text: indMensagem(S.ind) }); } catch (e) { /* cancelou */ }
}

async function indUsarCodigo() {
  const m = document.getElementById("ind-msg");
  m.className = "msg"; m.textContent = "Conferindo…";
  const r = await api("/cashback/api/indicacao/codigo", { codigo: document.getElementById("ind-outro").value });
  if (!r.ok) { m.className = "msg erro"; m.textContent = r.erro || "Não consegui usar o código."; return; }
  toast(`Pronto! ${r.quem || "Quem convidou você"} vai ganhar o desconto em dobro quando você abastecer pelo app.`, 4500);
  telaIndicar();
}

// ---------------------------------------------------------------- cadastro: campo do código (só com a campanha valendo)
async function indCampoCadastro() {
  const d = document.getElementById("c-convite-campo");
  if (!d) return;
  const r = await api("/cashback/api/indicacao/ativa");
  if (!r.ok || !r.ativo || !document.getElementById("c-convite-campo")) return;
  d.innerHTML = `<div class="campo"><label>Código de convite (opcional)</label><input id="c-convite" autocapitalize="characters" autocomplete="off" maxlength="16" placeholder="Ex.: MARIA123 (de quem indicou você)"></div>`;
}

TELAS.indicar = () => telaIndicar();
COM_BARRA.push("indicar");
