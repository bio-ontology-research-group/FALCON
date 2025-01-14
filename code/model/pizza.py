import torch
import pdb
import argparse
import os
import pandas as pd
import numpy as np
import random
from sklearn.metrics import roc_auc_score
from sklearn.metrics import average_precision_score
from sklearn.metrics import precision_recall_curve

def get_rights(right):
    ridges = []
    axiom_parts = right.split(' ')
    counter = 0
    for i, part in enumerate(axiom_parts):
        counter += part.count('(')
        counter -= part.count(')')
        if counter == 0:
            ridges.append(i)
    if len(ridges) == 1:
        return [right]
    elif len(ridges) > 1:
        rights = []
        for i in range(len(ridges)):
            if i == 0:
                rights.append(' '.join(axiom_parts[: ridges[0] + 1]))
            else:
                rights.append(' '.join(axiom_parts[ridges[i - 1] + 1: ridges[i] + 1]))
        return rights
    else:
        raise ValueError

def get_all_concepts_and_relations(axiom, all_concepts, all_relations):
    
    if axiom[0] == '<' or axiom == 'owl:Thing':
        all_concepts.add(axiom)
    
    elif axiom == 'owl:Nothing':
        pass
    
    elif axiom[:21] == 'ObjectIntersectionOf(':
        axiom_parts = axiom.split(' ')
        counter = 0
        for i, part in enumerate(axiom_parts):
            counter += part.count('(')
            counter -= part.count(')')
            if counter == 1:
                ridge = i
                break
        left = ' '.join(axiom_parts[:ridge + 1])[21:]
        get_all_concepts_and_relations(left, all_concepts, all_relations)
        right = ' '.join(axiom_parts[ridge + 1:])[:-1]
        rights = get_rights(right)
        for every_right in rights:
            get_all_concepts_and_relations(every_right, all_concepts, all_relations)
        
    elif axiom[:14] == 'ObjectUnionOf(':
        axiom_parts = axiom.split(' ')
        counter = 0
        for i, part in enumerate(axiom_parts):
            counter += part.count('(')
            counter -= part.count(')')
            if counter == 1:
                ridge = i
                break
        left = ' '.join(axiom_parts[:ridge + 1])[14:]
        get_all_concepts_and_relations(left, all_concepts, all_relations)
        right = ' '.join(axiom_parts[ridge + 1:])[:-1]
        rights = get_rights(right)
        for every_right in rights:
            get_all_concepts_and_relations(every_right, all_concepts, all_relations)
        
    elif axiom[:22] == 'ObjectSomeValuesFrom(<':
        axiom_parts = axiom.split(' ')
        all_relations.add(axiom_parts[0][21:])
        right = ' '.join(axiom_parts[1:])[:-1]
        get_all_concepts_and_relations(right, all_concepts, all_relations)
        
    elif axiom[:21] == 'ObjectAllValuesFrom(<':
        axiom_parts = axiom.split(' ')
        all_relations.add(axiom_parts[0][20:])
        right = ' '.join(axiom_parts[1:])[:-1]
        get_all_concepts_and_relations(right, all_concepts, all_relations)
        
    elif axiom[:19] == 'ObjectComplementOf(':
        get_all_concepts_and_relations(axiom[19:-1], all_concepts, all_relations)
        
    else:
        raise ValueError
    

class FALCON(torch.nn.Module):
    def __init__(self, c_dict, e_dict, r_dict, cfg):
        super().__init__()
        self.c_dict = c_dict
        self.e_dict = e_dict
        self.r_dict = r_dict
        self.n_entity = len(e_dict) + cfg.anon_e
        self.anon_e = cfg.anon_e
        self.c_embedding = torch.nn.Embedding(len(c_dict), cfg.emb_dim)
        self.r_embedding = torch.nn.Embedding(len(r_dict), cfg.emb_dim)
        self.e_embedding = torch.nn.Embedding(len(e_dict), cfg.emb_dim)
        self.fc_0 = torch.nn.Linear(cfg.emb_dim * 2, cfg.emb_dim)
        self.fc_1 = torch.nn.Linear(cfg.emb_dim, 1)
        torch.nn.init.xavier_uniform_(self.c_embedding.weight.data)
        torch.nn.init.xavier_uniform_(self.r_embedding.weight.data)
        torch.nn.init.xavier_uniform_(self.e_embedding.weight.data)
        torch.nn.init.xavier_uniform_(self.fc_0.weight.data)
        torch.nn.init.xavier_uniform_(self.fc_1.weight.data)
        self.max_measure = cfg.max_measure
        self.t_norm = cfg.t_norm
        self.nothing = torch.zeros(self.n_entity)
        self.residuum = cfg.residuum
    
    def _logical_and(self, x, y):
        if self.t_norm == 'product':
            return x * y
        elif self.t_norm == 'minmax':
            x = x.unsqueeze(dim=-2)
            y = y.expand_as(x)
            return torch.cat([x, y], dim=-2).min(dim=-2)[0]
        elif self.t_norm == 'Łukasiewicz':
            x = x.unsqueeze(dim=-2)
            y = y.expand_as(x)
            return (((x + y -1) > 0) * (x + y - 1)).squeeze(dim=-2)
        else:
            raise ValueError

    def _logical_or(self, x, y):
        if self.t_norm == 'product':
            return x + y - x * y
        elif self.t_norm == 'minmax':
            x = x.unsqueeze(dim=-2)
            y = y.expand_as(x)
            return torch.cat([x, y], dim=-2).max(dim=-2)[0]
        elif self.t_norm == 'Łukasiewicz':
            x = x.unsqueeze(dim=-2)
            y = y.expand_as(x)
            return 1 - ((((1-x) + (1-y) -1) > 0) * ((1-x) + (1-y) - 1)).squeeze(dim=-2)
        else:
            raise ValueError
    
    def _logical_not(self, x):
        return 1 - x
    
    def _logical_residuum(self, r_fs, c_fs):
        if self.residuum == 'notCorD':
            return self._logical_or(self._logical_not(r_fs), c_fs.unsqueeze(dim=-2))
        else:
            raise ValueError
    
    def _logical_exist(self, r_fs, c_fs):
        return self._logical_and(r_fs, c_fs).max(dim=-1)[0]

    def _logical_forall(self, r_fs, c_fs):
        return self._logical_residuum(r_fs, c_fs).min(dim=-1)[0]

    def _get_c_fs(self, c_emb, anon_e_emb):
        # e_emb = self.e_embedding.weight
        e_emb = torch.cat([self.e_embedding.weight, anon_e_emb], dim=0)
        c_emb = c_emb.expand_as(e_emb)
        emb = torch.cat([c_emb, e_emb], dim=-1)
        return torch.sigmoid(self.fc_1(torch.nn.functional.leaky_relu(self.fc_0(emb), negative_slope=0.1))).squeeze(dim=-1)

    def _get_r_fs(self, r_emb, anon_e_emb):
        e_emb = torch.cat([self.e_embedding.weight, anon_e_emb], dim=0)
        # e_emb = self.e_embedding.weight
        l_emb = (e_emb + r_emb.unsqueeze(dim=0)).unsqueeze(dim=1).repeat(1, self.n_entity, 1)
        r_emb = e_emb.unsqueeze(dim=0).repeat(self.n_entity, 1, 1)
        emb = torch.cat([l_emb, r_emb], dim=-1)
        return torch.sigmoid(self.fc_1(torch.nn.functional.leaky_relu(self.fc_0(emb), negative_slope=0.1))).squeeze(dim=-1)

    def forward(self, axiom, anon_e_emb):

        if axiom[0] == '<' or axiom == 'owl:Thing':
            c_emb = self.c_embedding(torch.tensor(self.c_dict[axiom]))
            return self._get_c_fs(c_emb, anon_e_emb)
        
        elif axiom == 'owl:Nothing':
            return self.nothing
        
        elif axiom[:21] == 'ObjectIntersectionOf(':
            axiom_parts = axiom.split(' ')
            counter = 0
            for i, part in enumerate(axiom_parts):
                counter += part.count('(')
                counter -= part.count(')')
                if counter == 1:
                    ridge = i
                    break
            left = ' '.join(axiom_parts[:ridge + 1])[21:]
            right = ' '.join(axiom_parts[ridge + 1:])[:-1]
            rights = get_rights(right)
            ret = self._logical_and(self.forward(left, anon_e_emb), self.forward(rights[0], anon_e_emb))
            if len(rights) > 1:
                for every_right_id in range(1, len(rights)):
                    ret = self._logical_and(ret, self.forward(rights[every_right_id], anon_e_emb))
            return ret

        elif axiom[:14] == 'ObjectUnionOf(':
            axiom_parts = axiom.split(' ')
            counter = 0
            for i, part in enumerate(axiom_parts):
                counter += part.count('(')
                counter -= part.count(')')
                if counter == 1:
                    ridge = i
                    break
            left = ' '.join(axiom_parts[:ridge + 1])[14:]
            right = ' '.join(axiom_parts[ridge + 1:])[:-1]
            rights = get_rights(right)
            ret = self._logical_or(self.forward(left, anon_e_emb), self.forward(rights[0], anon_e_emb))
            if len(rights) > 1:
                for every_right_id in range(1, len(rights)):
                    ret = self._logical_or(ret, self.forward(rights[every_right_id], anon_e_emb))
            return ret

        elif axiom[:22] == 'ObjectSomeValuesFrom(<':
            axiom_parts = axiom.split(' ')
            r_emb = self.r_embedding(torch.tensor(self.r_dict[axiom_parts[0][21:]]))
            r_fs = self._get_r_fs(r_emb, anon_e_emb)
            right = ' '.join(axiom_parts[1:])[:-1]
            return self._logical_exist(r_fs, self.forward(right, anon_e_emb))

        elif axiom[:21] == 'ObjectAllValuesFrom(<':
            axiom_parts = axiom.split(' ')
            r_emb = self.r_embedding(torch.tensor(self.r_dict[axiom_parts[0][20:]]))
            r_fs = self._get_r_fs(r_emb, anon_e_emb)
            right = ' '.join(axiom_parts[1:])[:-1]
            return self._logical_forall(r_fs, self.forward(right, anon_e_emb))

        elif axiom[:19] == 'ObjectComplementOf(':
            return self._logical_not(self.forward(axiom[19:-1], anon_e_emb))

        else:
            pdb.set_trace()
            raise ValueError

    def get_cc_loss(self, fs):
        if self.max_measure == 'max':
            return - torch.log(1 - fs.max(dim=-1)[0] + 1e-10)
        elif self.max_measure[:5] == 'pmean':
            p = int(self.max_measure[-1])
            return ((fs ** p).mean(dim=-1))**(1/p)
        else:
            raise ValueError

    def get_ec_loss(self, axiom):
        axiom = axiom.split(' ')
        e, c = axiom[0], axiom[1]
        e_id = self.e_dict[e]
        c_id = self.c_dict[c]
        e_emb = self.e_embedding.weight[e_id]
        c_emb = self.c_embedding.weight[c_id]
        emb = torch.cat([c_emb, e_emb])
        dofm = torch.sigmoid(self.fc_1(torch.nn.functional.leaky_relu(self.fc_0(emb), negative_slope=0.1))).squeeze(dim=-1)
        return - torch.log(dofm + 1e-10) 

    def get_ee_loss(self, axiom):
        axiom = axiom.split(' ')
        e_1, r, e_2 = axiom[0], axiom[1], axiom[2]
        e_1_id = self.e_dict[e_1]
        r_id = self.r_dict[r]
        e_2_id = self.e_dict[e_2]
        e_1_emb = self.e_embedding.weight[e_1_id]
        r_emb = self.r_embedding.weight[r_id]
        e_2_emb = self.e_embedding.weight[e_2_id]
        emb = torch.cat([e_1_emb + r_emb, e_2_emb])
        dofm = torch.sigmoid(self.fc_1(torch.nn.functional.leaky_relu(self.fc_0(emb), negative_slope=0.1))).squeeze(dim=-1)
        
        emb_neg = torch.cat([e_2_emb + r_emb, e_1_emb])
        dofm_neg = torch.sigmoid(self.fc_1(torch.nn.functional.leaky_relu(self.fc_0(emb_neg), negative_slope=0.1))).squeeze(dim=-1)
        return - torch.log(dofm + 1e-10) - torch.log(1 - dofm_neg + 1e-10) 

def extract_nodes(cfg, filename, moreAxioms=[]):
    outliers = ['ObjectOneOf', 'ObjectHasValue', 'ObjectMinCardinality']
    tbox = []
    with open(cfg.data_root + filename) as f:
        for line in f:
            line = line.strip('\n')
            flag = 0
            for outlier in outliers:
                if outlier in line:
                    flag = 1
                    break
            if flag == 0:
                tbox.append(line)
    for moreAxiom in moreAxioms:
        tbox.append(moreAxiom)
    all_concepts = set()
    all_relations = set()
    for axiom in tbox:
        get_all_concepts_and_relations(axiom, all_concepts, all_relations)
    return tbox, all_concepts, all_relations

def extract_concepts_in_abox_ec(abox_ec):
    ret = set()
    for axiom in abox_ec:
        ret.add(axiom.split(' ')[-1])
    return ret

def concept_replacer(axiom, all_concepts):
    
    if (axiom[0] == '<') or (axiom == 'owl:Nothing') or (axiom == 'owl:Thing'):
        ret = random.choice(all_concepts)
        while 'Mysterious' in ret:
            ret = random.choice(all_concepts)
        return ret
    
    elif axiom[:21] == 'ObjectIntersectionOf(':
        axiom_parts = axiom.split(' ')
        counter = 0
        for i, part in enumerate(axiom_parts):
            counter += part.count('(')
            counter -= part.count(')')
            if counter == 1:
                ridge = i
                break
        left = ' '.join(axiom_parts[:ridge + 1])[21:]
        left_replaced = concept_replacer(left, all_concepts)
        ret = [left_replaced]
        right = ' '.join(axiom_parts[ridge + 1:])[:-1]
        rights = get_rights(right)
        for every_right in rights:
            ret.append(concept_replacer(every_right, all_concepts))
        return 'ObjectIntersectionOf(' + ' '.join(ret) + ')'
        
    elif axiom[:14] == 'ObjectUnionOf(':
        axiom_parts = axiom.split(' ')
        counter = 0
        for i, part in enumerate(axiom_parts):
            counter += part.count('(')
            counter -= part.count(')')
            if counter == 1:
                ridge = i
                break
        left = ' '.join(axiom_parts[:ridge + 1])[14:]
        left_replaced = concept_replacer(left, all_concepts)
        ret = [left_replaced]
        right = ' '.join(axiom_parts[ridge + 1:])[:-1]
        rights = get_rights(right)
        for every_right in rights:
            ret.append(concept_replacer(every_right, all_concepts))
        return 'ObjectUnionOf(' + ' '.join(ret) + ')'
        
    elif axiom[:22] == 'ObjectSomeValuesFrom(<':
        axiom_parts = axiom.split(' ')
        right = ' '.join(axiom_parts[1:])[:-1]
        return axiom_parts[0] + ' ' + concept_replacer(right, all_concepts) + ')'
        
    elif axiom[:21] == 'ObjectAllValuesFrom(<':
        axiom_parts = axiom.split(' ')
        right = ' '.join(axiom_parts[1:])[:-1]
        return axiom_parts[0] + ' ' + concept_replacer(right, all_concepts) + ')'
    
    elif axiom[:19] == 'ObjectComplementOf(':
        return 'ObjectComplementOf(' + concept_replacer(axiom[19:-1], all_concepts) + ')'
        
    else:
        raise ValueError

def tbox_test_neg_generator(tbox, all_concepts, k):
    ret = []
    for _ in range(k):
        axiom_pos = random.choice(tbox)
        axiom_neg = concept_replacer(axiom_pos, all_concepts)
        while axiom_neg in tbox:
            axiom_neg = concept_replacer(axiom_pos, all_concepts)
        ret.append(axiom_neg)
    return ret

def get_data(cfg):
    moreAxioms = [
        'ObjectIntersectionOf(<MysteriousPizza_0> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza> <http://www.co-ode.org/ontologies/pizza/pizza.owl#VegetarianPizza>)))',
        'ObjectIntersectionOf(<MysteriousPizza_1> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#NonVegetarianPizza> <http://www.co-ode.org/ontologies/pizza/pizza.owl#VegetarianPizza>)))',
        'ObjectIntersectionOf(<MysteriousPizza_2> ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza>)))',
        'ObjectIntersectionOf(<MysteriousPizza_3> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza> <http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream>)))',
        'ObjectIntersectionOf(<MysteriousTopping_0> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatTopping> <http://www.co-ode.org/ontologies/pizza/pizza.owl#VegetarianTopping>)))',
        'ObjectIntersectionOf(<MysteriousTopping_1> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatTopping> <http://www.co-ode.org/ontologies/pizza/pizza.owl#VegetableTopping>)))',
        'ObjectIntersectionOf(<MysteriousTopping_2> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#SeafoodTopping> <http://www.co-ode.org/ontologies/pizza/pizza.owl#VegetarianTopping>)))',
        'ObjectIntersectionOf(<MysteriousTopping_3> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping>))))',
        'ObjectIntersectionOf(<MysteriousBase_0> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#DeepPanBase> <http://www.co-ode.org/ontologies/pizza/pizza.owl#ThinAndCrispyBase>)))',
        'ObjectIntersectionOf(<MysteriousBase_1> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase>))))',
        
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza>))))',
        
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza>))))',
        
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza>))))',
        
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza>))))',
        
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza>))))',
        
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatTopping> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatTopping> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatTopping> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatTopping> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatTopping> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza>))))',

        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#VegetarianTopping> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#VegetarianTopping> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#VegetarianTopping> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#VegetarianTopping> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#VegetarianTopping> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza>))))',
        
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#DeepPanBase> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaTopping>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#DeepPanBase> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#PizzaBase>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#DeepPanBase> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#Pizza>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#DeepPanBase> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#IceCream>))))',
        'ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#DeepPanBase> ObjectComplementOf(ObjectIntersectionOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza> ObjectComplementOf(<http://www.co-ode.org/ontologies/pizza/pizza.owl#MeatyPizza>))))',
    ]
    moreAxioms = (moreAxioms * 20)[:cfg.n_inconsistent]
    tbox_train, all_concepts_train, all_relations_train = extract_nodes(cfg, filename='pizzaTBox.txt', moreAxioms=moreAxioms)
    tbox_all, all_concepts_test, all_relations_test = extract_nodes(cfg, filename='pizzaTBoxInferred.txt')
    tbox_test_pos = []
    for axiom in tbox_all:
        if axiom not in tbox_train:
            tbox_test_pos.append(axiom)
    
    all_concepts = all_concepts_train | all_concepts_test
    all_relations = all_relations_train | all_relations_test
    abox_ec = [
        '<http://www.co-ode.org/ontologies/pizza/pizza.owl#America> <http://www.co-ode.org/ontologies/pizza/pizza.owl#Country>',
        '<http://www.co-ode.org/ontologies/pizza/pizza.owl#England> <http://www.co-ode.org/ontologies/pizza/pizza.owl#Country>',
        '<http://www.co-ode.org/ontologies/pizza/pizza.owl#France> <http://www.co-ode.org/ontologies/pizza/pizza.owl#Country>',
        '<http://www.co-ode.org/ontologies/pizza/pizza.owl#Italy> <http://www.co-ode.org/ontologies/pizza/pizza.owl#Country>',
        '<http://www.co-ode.org/ontologies/pizza/pizza.owl#Germany> <http://www.co-ode.org/ontologies/pizza/pizza.owl#Country>',
    ]
    abox_ee = [
    ]
    concepts_in_abox_ec = extract_concepts_in_abox_ec(abox_ec)
    c_dict = {k: v for v, k in enumerate(all_concepts)}
    r_dict = {k: v for v, k in enumerate(all_relations)}
    for concept in all_concepts:
        if concept not in concepts_in_abox_ec:
            abox_ec.append(concept[:-1] + '_1> ' + concept)
    
    all_entities = set()
    for axiom in abox_ec:
        all_entities.add(axiom.split(' ')[0])
    for axiom in abox_ee:
        all_entities.add(axiom.split(' ')[0])
        all_entities.add(axiom.split(' ')[-1])
    e_dict = {k: v for v, k in enumerate(all_entities)}
    
    try:
        tbox_test_neg = []
        with open(cfg.data_root + 'tbox_test_neg.txt') as f:
            for line in f:
                tbox_test_neg.append(line.strip('\n'))
    except:
        tbox_test_neg = tbox_test_neg_generator(tbox_all, list(all_concepts), k=len(tbox_test_pos))
        with open(cfg.data_root + 'tbox_test_neg.txt', 'w') as f:
            for axiom in tbox_test_neg:
                f.write("%s\n" % axiom)
    
    return tbox_train, tbox_test_pos, tbox_test_neg, abox_ec, abox_ee, c_dict, e_dict, r_dict

def compute_metrics(preds):
    n_pos = n_neg = len(preds) // 2
    labels = [0] * n_pos + [1] * n_neg
    
    mae_pos = round(sum(preds[:n_pos]) / n_pos, 4)
    auc = round(roc_auc_score(labels, preds), 4)
    aupr = round(average_precision_score(labels, preds), 4)
    
    precision, recall, _ = precision_recall_curve(labels, preds)
    f1_scores = 2 * recall * precision / (recall + precision + 1e-10)
    fmax = round(np.max(f1_scores), 4)
    
    return mae_pos, auc, aupr, fmax

def parse_args(args=None):
    parser = argparse.ArgumentParser()
    # Tuable
    parser.add_argument('--lr', default=0.005, type=float)
    parser.add_argument('--wd', default=0, type=float)
    parser.add_argument('--emb_dim', default=50, type=int)
    parser.add_argument('--n_models', default=5, type=int)
    parser.add_argument('--bs', default=256, type=int)
    parser.add_argument('--anon_e', default=0, type=int)
    parser.add_argument('--n_inconsistent', default=0, type=int)
    parser.add_argument('--t_norm', default='product', type=str, help='product, minmax, Łukasiewicz, or NN')
    parser.add_argument('--residuum', default='notCorD', type=str)
    parser.add_argument('--max_measure', default='max', type=str)
    # Untunable
    parser.add_argument('--data_root', default='../../data/Pizza/', type=str)
    parser.add_argument('--max_steps', default=10000, type=int)
    parser.add_argument('--valid_interval', default=10, type=int)
    parser.add_argument('--tolerance', default=5, type=int)
    return parser.parse_args(args)

if __name__ == '__main__':
    cfg = parse_args()
    print('Configurations:')
    for arg in vars(cfg):
        print(f'\t{arg}: {getattr(cfg, arg)}', flush=True)
    tbox_train, tbox_test_pos, tbox_test_neg, abox_ec, abox_ee, c_dict, e_dict, r_dict = get_data(cfg)
    print(f'TBox train:{len(tbox_train)}, TBox test pos:{len(tbox_test_pos)}, TBox test neg:{len(tbox_test_neg)}')
    
    save_root = f'../tmp/lr_{cfg.lr}_wd_{cfg.wd}_emb_dim_{cfg.emb_dim}_n_models_{cfg.n_models}_bs_{cfg.bs}_anon_e_{cfg.anon_e}_n_inconsistent_{cfg.n_inconsistent}_t_norm_{cfg.t_norm}_residuum_{cfg.residuum}_max_measure_{cfg.max_measure}/'
    if not os.path.exists(save_root):
        os.makedirs(save_root)
        
    logits = []
    mae_poss, aucs, auprs, fmaxs = 0, 0, 0, 0
    for i in range(cfg.n_models):
        print(f'Model {i+1}', flush=True)
        model = FALCON(c_dict=c_dict, 
                        e_dict=e_dict, 
                        r_dict=r_dict, 
                        cfg=cfg)
        optimizer = torch.optim.Adam(model.parameters(), lr=cfg.lr, weight_decay=cfg.wd)
        tolerance = cfg.tolerance
        best_value = 0
        for step in range(cfg.max_steps):
            # print(f'Step {step + 1}:')
            model.train()
            anon_e_emb_1 = model.e_embedding.weight.detach()[:cfg.anon_e//2] + torch.normal(0, 0.1, size=(cfg.anon_e//2, cfg.emb_dim))
            anon_e_emb_2 = torch.rand(cfg.anon_e//2, cfg.emb_dim)
            torch.nn.init.xavier_uniform_(anon_e_emb_2)
            anon_e_emb = torch.cat([anon_e_emb_1, anon_e_emb_2], dim=0)
            loss_cc = []
            loss_ec = []
            loss_ee = []
            batch_tbox_train = random.sample(tbox_train, cfg.bs)
            batch_abox_ec = random.sample(abox_ec, cfg.bs//4)
            for axiom in batch_tbox_train:
                fs = model.forward(axiom, anon_e_emb)
                loss_cc.append(model.get_cc_loss(fs))
            for axiom in batch_abox_ec:
                loss_ec.append(model.get_ec_loss(axiom))
            # for axiom in abox_ee:
            #     loss_ee.append(model.get_ee_loss(axiom))
            loss = 0.5 * (sum(loss_cc) / len(loss_cc)) + 0.5 * (sum(loss_ec) / len(loss_ec)) 
            # + 0.2 * (sum(loss_ee) / len(loss_ee)) 
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            # print(f'Loss: {round(loss.item(), 4)}')
            if (step + 1) % cfg.valid_interval == 0:
                model.eval()
                preds = []
                with torch.no_grad():
                    for axiom in tbox_test_pos:
                        fs = model.forward(axiom, anon_e_emb)
                        preds.append(max(fs).item())
                    for axiom in tbox_test_neg:
                        fs = model.forward(axiom, anon_e_emb)
                        preds.append(max(fs).item())
                mae_pos, auc, aupr, fmax = compute_metrics(preds)
                # print(f'MAE:{mae_pos}\tAUC:{auc}\tAUPR:{aupr}\tFmax:{fmax}')
                early_stop_value = auc
                if early_stop_value >= best_value:
                    best_value = early_stop_value
                    tolerance = cfg.tolerance
                else:
                    tolerance -= 1
                torch.save(model.state_dict(), save_root + (str(step + 1)))
            if (tolerance == 0) or ((step + 1) == cfg.max_steps):
                break
            
        logits_each_model = []
        model.load_state_dict(torch.load(save_root + str(step - cfg.tolerance * cfg.valid_interval + 1)))
        with torch.no_grad():
            model.eval()
            for axiom in tbox_test_pos:
                fs = model.forward(axiom, anon_e_emb)
                logits_each_model.append(max(fs).item())
            for axiom in tbox_test_neg:
                fs = model.forward(axiom, anon_e_emb)
                logits_each_model.append(max(fs).item())
        logits.append(logits_each_model)

        mae_pos, auc, aupr, fmax = compute_metrics(logits_each_model)
        mae_poss += mae_pos
        aucs += auc
        auprs += aupr
        fmaxs += fmax
        mae_pos = round(mae_poss/(i + 1), 4)
        auc = round(aucs/(i + 1), 4)
        aupr = round(auprs/(i + 1), 4)
        fmax = round(fmaxs/(i + 1), 4)
        print(f'AVG: MAE:{mae_pos}\tAUC:{auc}\tAUPR:{aupr}\tFmax:{fmax}', flush=True)    

        preds = torch.tensor(logits).max(dim=0)[0].numpy().tolist()
        mae_pos, auc, aupr, fmax = compute_metrics(preds)
        print(f'MAX: MAE:{mae_pos}\tAUC:{auc}\tAUPR:{aupr}\tFmax:{fmax}', flush=True)    
