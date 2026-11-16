import {batches,inspections,reviews,merges,tonnage} from '../api/business.js';
export const state={user:null,batches:[],inspections:[],reviews:[],merges:[],tonnage:null,selected:null};
export async function refresh(){
  [state.batches,state.inspections,state.reviews,state.merges,state.tonnage]=
    await Promise.all([batches.list(),inspections.list(),reviews.list(),merges.list(),tonnage()]);
  if(state.selected)state.selected=await reviews.get(state.selected.id);
}
