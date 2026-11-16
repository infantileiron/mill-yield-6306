/** @typedef {{id:number,code:string,line:string,supplier:string,tonnes:number,received_date:string,samples:number,group_id:number|null,group_code:string|null,group_status:string|null,is_primary:boolean,verified_tonnes:number|null}} Batch */
/** @typedef {{sample_code:string,batch_id:number,entry_batch_id:number,entry_batch_code:string,group_id:number|null,group_code:string|null,flour_yield:number,moisture:number,inspector:string}} Inspection */
/** @typedef {{id:number,title:string,line:string,status:string,result:object,context:object,note:string}} Review */
/** @typedef {{id:number,code:string,primary_batch_id:number,primary_code:string,verified_tonnes:number,status:string,members:Array,member_count:number,total_samples:number,sum_member_tonnes:number}} BatchGroup */
export const API_VERSION='2';
