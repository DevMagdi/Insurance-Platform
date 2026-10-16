import { Link } from 'react-router-dom';
import { Button } from '../components/ui/Button';
export default function NotFound(){return <main className="grid min-h-[60vh] place-items-center px-5 text-center"><div><p className="text-7xl font-black text-emerald-600">404</p><h1 className="mt-4 text-2xl font-bold">الصفحة غير موجودة</h1><p className="mt-2 text-slate-500">تحقق من الرابط أو عد إلى الصفحة الرئيسية.</p><Link to="/"><Button className="mt-6">العودة للرئيسية</Button></Link></div></main>}
