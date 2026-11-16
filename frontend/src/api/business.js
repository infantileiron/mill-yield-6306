import {api, post, download} from './client.js';
export const batches={
  list:()=>api('/batches'),
  create:d=>post('/batches',d),
  attribution:id=>api('/batches/'+id+'/attribution'),
};
export const inspections={list:()=>api('/inspections'),create:d=>post('/inspections',d)};
export const reviews={list:()=>api('/reviews'),get:id=>api('/reviews/'+id),create:d=>post('/reviews',d),approve:(id,note)=>post(`/reviews/${id}/approve`,{note}),download};
export const merges={
  list:()=>api('/merges'),
  preview:batch_ids=>post('/merges/preview',{batch_ids}),
  confirm:d=>post('/merges/confirm',d),
  dissolve:id=>post(`/merges/${id}/dissolve`),
};
export const tonnage=line=>api('/tonnage'+(line?`?line=${encodeURIComponent(line)}`:''));
