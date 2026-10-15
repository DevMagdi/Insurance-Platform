/** Phase 1 shell only: journeys and dashboard screens are scheduled for later phases. */
function App() {
  return (
    <main className="min-h-screen bg-[#081a2f] px-6 py-16 text-slate-100">
      <div className="mx-auto max-w-3xl rounded-3xl border border-amber-200/20 bg-white/5 p-10 shadow-2xl backdrop-blur-xl">
        <p className="mb-6 text-sm tracking-[0.22em] text-amber-300" lang="en" dir="ltr">
          INSURANCE • COMPLAINT MANAGEMENT
        </p>
        <h1 className="text-3xl font-semibold leading-relaxed text-white">
          إدارة شكاوى التأمين، برؤية أوضح
        </h1>
        <p className="mt-5 leading-8 text-slate-300">
          المرحلة الأولى: البنية الأساسية ونموذج البيانات. بوابة العملاء ولوحة فريق الوساطة قيد التطوير.
        </p>
        <p className="mt-8 border-t border-white/10 pt-6 text-sm text-emerald-300" lang="en" dir="ltr">
          Phase 1 foundation · Customer and brokerage journeys coming next
        </p>
      </div>
    </main>
  );
}

export default App;
