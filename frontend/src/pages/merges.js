import {table} from '../components/common/table.js';
import {esc,fmt,canEdit} from '../utils/format.js';
import {merges,batches} from '../api/business.js';
import {notify} from '../hooks/forms.js';

export function idemKey(){
  if(window.crypto?.randomUUID)return 'm-'+crypto.randomUUID().replaceAll('-','').slice(0,24);
  return 'm-'+Date.now().toString(36)+Math.random().toString(36).slice(2,18);
}

export function mergesPage(s){
  const candidates=s.batches.filter(b=>!b.group_id);
  const groups=s.merges||[];
  const active=groups.filter(g=>g.status==='active');
  const gross=s.tonnage?.gross_tonnes??0, dedup=s.tonnage?.deduped_tonnes??0;
  return `<header><p class="eyebrow">02 / BATCH MERGE</p><h1>批次归并</h1><p>同一车原粮误建多个批号时先预览成员、样本数与核实吨数，确认后归并组只计一次吨数，样本各计一次；旧批号仍可补录。</p></header>
<div class="workspace merges-workspace">
  <section class="paper">
    <h2>归并预览与确认</h2>
    <form id="merge-form">
      <label>选择批号（同产线、同供应商，至少 2 个）
        <select id="merge-picker" multiple size="7">${candidates.map(b=>`<option value="${b.id}">${esc(b.code)} · ${esc(b.line)} · ${esc(b.supplier)} · ${fmt(b.tonnes,1)}t · ${b.samples}样本</option>`).join('')}</select>
      </label>
      <small class="hint">按住 Ctrl / ⌘ 多选；已在生效归并组中的批号不在候选内。</small>
      <button type="button" class="outline" id="merge-preview-btn">预览归并 →</button>
      <div id="merge-preview" class="merge-preview"></div>
      <p class="hint">确认前可修改归并组编号与核实吨数；同号重试不会重复生效。</p>
    </form>
  </section>
  <section>
    <div class="section-head"><h2>归并组台账</h2><span>${active.length} 个生效 / 共 ${groups.length} 组</span></div>
    <div class="tonnage-cards">
      <div><small>台账毛吨数（重复统计）</small><b>${fmt(gross,1)} t</b></div>
      <div><small>归并去重核实吨数</small><b class="rust">${fmt(dedup,1)} t</b></div>
      <div><small>去重差额</small><b>${fmt(gross-dedup,1)} t</b></div>
    </div>
    ${table(['归并组 / 主批号','成员批号','样本（各计一次）','核实吨数（只计一次）','状态 / 操作'],groups.map(g=>[
      `<strong>${esc(g.code)}</strong><small>主批号 ${esc(g.primary_code)}</small>`,
      g.members.map(m=>`<span class="chip">${esc(m.code)}${m.is_primary?' ★':''}</span>`).join(''),
      `${g.total_samples} 个 / ${g.member_count} 批`,
      `${fmt(g.verified_tonnes,1)} t<small>成员台账合计 ${fmt(g.sum_member_tonnes,1)}t</small>`,
      g.status==='active'
        ? `<span class="badge completed">生效中</span><br><button class="text danger" data-dissolve="${g.id}" ${canEdit(s.user)?'':'disabled'}>解除归并</button>`
        : `<span class="badge failed">已解除</span>`,
    ]))}
    <h3>批号归属来历</h3>
    <div class="attrib-row">
      <select id="attrib-picker">${s.batches.map(b=>`<option value="${b.id}">${esc(b.code)}</option>`).join('')}</select>
      <button class="outline" id="attrib-btn" type="button">查归属来历</button>
    </div>
    <div id="attrib-panel" class="attrib-panel"></div>
  </section>
</div>`;
}

export function bindMerges(s,render){
  const picker=document.getElementById('merge-picker');
  const panel=document.getElementById('merge-preview');
  const form=document.getElementById('merge-form');
  let currentKey=idemKey();
  const selected=()=>[...picker.selectedOptions].map(o=>Number(o.value));
  document.getElementById('merge-preview-btn').onclick=async()=>{
    const ids=selected();
    if(ids.length<2){notify('请至少选择两个批号',true);return;}
    try{
      const p=await merges.preview(ids);
      panel.innerHTML=`<div class="preview-head ${p.ready?'ok':'blocked'}">${p.ready?'符合归并条件，可确认':'暂不可归并：'+(p.blocked_by_active_group.length?'批号已在其他归并组；':'')+(p.same_line?'':'产线不一致；')+(p.same_supplier?'':'供应商不一致；')}</div>
      ${table(['批号','产线','供应商','吨数','样本数'],p.members.map(m=>[esc(m.code),esc(m.line),esc(m.supplier),fmt(m.tonnes,1)+' t',m.samples]))}
      <div class="preview-metrics"><span>成员 <b>${p.member_count}</b> 批</span><span>样本合计 <b>${p.total_samples}</b> 个（各计一次）</span><span>成员吨数合计 <b>${fmt(p.sum_member_tonnes,1)}</b> t</span></div>
      <label>归并组编号<input name="code" maxlength="50" value="${esc(p.proposed_group_code)}" pattern="[A-Za-z0-9_\\-]+"></label>
      <div class="form-row"><label>主批号<select name="primary_batch_id">${p.members.map(m=>`<option value="${m.batch_id}" ${m.batch_id===p.primary_batch_id?'selected':''}>${esc(m.code)}</option>`).join('')}</select></label>
      <label>核实吨数 t<input name="verified_tonnes" type="number" min="0.1" step="0.1" value="${fmt(p.verified_tonnes,1)}"></label></div>
      <label>备注（可选）<input name="note" maxlength="500" placeholder="同一车原粮误建批号，归并去重"></label>
      <button class="primary" ${p.ready&&canEdit(s.user)?'':'disabled'}>确认归并（吨数只计一次）</button>`;
    }catch(e){notify(e.message,true);}
  };
  if(form)form.onsubmit=async e=>{
    e.preventDefault();
    const btn=form.querySelector('button.primary');
    if(btn)btn.disabled=true;
    const fd=new FormData(form);
    try{
      const payload={
        batch_ids:selected(),
        idempotency_key:currentKey,
        code:String(fd.get('code')||'').trim(),
        primary_batch_id:Number(fd.get('primary_batch_id')),
        verified_tonnes:Number(fd.get('verified_tonnes')),
        note:String(fd.get('note')||''),
      };
      const r=await merges.confirm(payload);
      notify(r.idempotent_reused?'该归并已生效（幂等重试，未重复创建）。':'归并已确认，吨数按归并组只计一次。');
      currentKey=idemKey();
      await render();
    }catch(err){notify(err.message,true);if(btn)btn.disabled=false;}
  };
  document.querySelectorAll('[data-dissolve]').forEach(btn=>{
    btn.onclick=async()=>{
      const id=btn.dataset.dissolve;
      if(!confirm('解除后成员批号（含归并期间补录的样本）按原入口批号还原；若存在后续归并需先解除依赖。确认解除？'))return;
      btn.disabled=true;
      try{
        const r=await merges.dissolve(id);
        notify(r.idempotent_reused?'该归并组此前已解除，未重复操作。':'归并已解除，成员批号已还原。');
        await render();
      }catch(e){notify(e.message,true);btn.disabled=false;}
    };
  });
  const attribBtn=document.getElementById('attrib-btn');
  if(attribBtn)attribBtn.onclick=async()=>{
    const id=Number(document.getElementById('attrib-picker').value);
    try{
      const a=await batches.attribution(id);
      const ap=document.getElementById('attrib-panel');
      ap.innerHTML=`<div class="attrib-current">当前：<strong>${esc(a.batch.code)}</strong> ${a.active_group?`归属生效归并组 <span class="chip">${esc(a.active_group.code)}</span>，核实吨数 ${fmt(a.active_group.verified_tonnes,1)}t`:'<em>独立批号，未归并</em>'}</div>`+
        (a.history.length?table(['归并组','状态','主批号','成员批号','加入时间','解除时间'],a.history.map(h=>[esc(h.group_code),h.status==='active'?'<span class="badge completed">生效中</span>':'<span class="badge failed">已解除</span>',esc(h.primary_code)+(h.is_primary?' ★':''),h.member_codes.map(c=>`<span class="chip">${esc(c)}</span>`).join(''),esc(h.joined_at.slice(0,16).replace('T',' ')),h.dissolved_at?esc(h.dissolved_at.slice(0,16).replace('T',' ')):'—'])):'<p class="hint">该批号没有归并履历。</p>');
    }catch(e){notify(e.message,true);}
  };
}
