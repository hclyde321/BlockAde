/* Local topic suggestions: evidence is a title or a transcript excerpt, never an inferred quote. */
window.BlockAdePBLMatch = (() => {
  const concepts = [
    ['發炎','炎症','inflammation','inflammatory','pneumonitis'],
    ['細胞傷害','細胞死亡','cell injury','cell death','necrosis','壞死'],
    ['組織修復','傷口','癒合','wound','healing','repair'],
    ['血行動力','體液循環','血栓','栓塞','thrombosis','embolism','hemodynamic'],
    ['水腫','edema'],['休克','shock'],['敗血症','sepsis'],
    ['感染','infection','infectious'],['抗生素','抗微生物','antibiotic','antimicrobial','imipenem','amikacin'],
    ['免疫','immune','immunity','immunotherapy'],['腫瘤','癌症','癌','tumor','cancer','neoplasm'],
    ['細胞適應','cell adaptation'],['病理','pathology','histology','切片'],
    ['手術','surgery','surgical'],['燒傷','burn'],['病史','身體診察','physical examination','history taking'],
    ['病歷','medical record'],['藥物濃度','drug monitoring','therapeutic drug'],
    ['重點照護','poct'],['體腔液','胸水','pleural effusion','腹水','ascites'],
    ['分子檢驗','molecular diagnosis'],['檢驗結果','檢驗品質'],
    ['自泌素','autacoid','histamine','組織胺','prostaglandin','前列腺素'],
    ['膽素','cholinergic','acetylcholine'],['腎素能','adrenergic','adrenaline'],
    ['離子管道','ion channel'],['心臟','心肌','cardiac','heart','myocardial'],
    ['血管','vascular','動脈','artery'],['肺','呼吸','lung','pulmonary','respiratory'],
    ['消化','胃腸','gastrointestinal','digestive'],['糖尿病','diabetes'],
    ['肥胖','obesity'],['肝','liver','hepatitis'],['腎臟','renal','kidney'],
    ['貧血','anemia'],['類固醇','steroid','prednisolone','dexamethasone'],
    ['結核','tuberculosis'],['肉芽腫','granuloma'],['潰瘍性結腸炎','ulcerative colitis']
  ];
  const normalize = text => String(text||'').toLowerCase().replace(/\s+/g,' ');
  const hits = (text, aliases) => aliases.some(term=> /[a-z]/i.test(term)
    ? new RegExp(`\\b${term}\\b`,'i').test(text) : text.includes(term));
  function match(text, courses) {
    if(!text)return [];
    const relevant=concepts.filter(c=>hits(normalize(text),c));
    return courses.flatMap(course=>{
      const title=normalize(course.title);
      const titleTopics=relevant.filter(c=>hits(title,c));
      const excerpts=(course.segments||[]).map(s=>{
        const topics=relevant.filter(c=>hits(normalize(s.text),c));
        return {text:s.text,start:Number(s.start??s.start_ms/1000)||0,topics:topics.map(c=>c[0]),score:topics.length};
      }).filter(s=>s.score>=2).sort((a,b)=>b.score-a.score||a.start-b.start).slice(0,2);
      // Generic pathology alone does not establish a useful match.
      const specific=titleTopics.filter(c=>c[0]!=='病理');
      if(!specific.length&&!excerpts.length)return [];
      return [{id:course.id,title:course.title,subject_id:course.subject_id,
        topics:[...new Set([...specific.map(c=>c[0]),...excerpts.flatMap(e=>e.topics)])],
        evidence:excerpts.length?'逐字稿':'課名待核對',excerpts,
        score:specific.length*3+excerpts.reduce((n,e)=>n+e.score,0)}];
    }).sort((a,b)=>b.score-a.score||a.title.localeCompare(b.title));
  }
  return {match};
})();
